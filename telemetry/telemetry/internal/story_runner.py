# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import contextlib
import itertools
import logging
import optparse
import os
import shutil
import sys
import time

import py_utils
from py_utils import cloud_storage  # pylint: disable=import-error
from py_utils import logging_util  # pylint: disable=import-error

from telemetry.core import exceptions
from telemetry.internal.actions import page_action
from telemetry.internal.browser import browser_finder
from telemetry.internal.browser import browser_finder_exceptions
from telemetry.internal.results import results_options
from telemetry.internal.results import results_processor
from telemetry.internal.util import exception_formatter
from telemetry import page
from telemetry.page import legacy_page_test
from telemetry.story import story_filter as story_filter_module
from telemetry.util import wpr_modes
from telemetry.web_perf import story_test


# Allowed stages to pause for user interaction at.
_PAUSE_STAGES = ('before-start-browser', 'after-start-browser',
                 'before-run-story', 'after-run-story')

_UNHANDLEABLE_ERRORS = (
    SystemExit,
    KeyboardInterrupt,
    ImportError,
    MemoryError)


class ArchiveError(Exception):
  pass


def AddCommandLineArgs(parser):
  story_filter_module.StoryFilterFactory.AddCommandLineArgs(parser)

  group = optparse.OptionGroup(parser, 'Story runner options')
  # Note that the default for pageset-repeat is 1 unless the benchmark
  # specifies a different default by adding
  # `options = {'pageset_repeat': X}` in their benchmark. Defaults are always
  # overridden by passed in commandline arguments.
  group.add_option('--pageset-repeat', default=1, type='int',
                   help='Number of times to repeat the entire pageset. ')
  # TODO(crbug.com/910809): Add flag to reduce iterations to 1.
  # (An iteration is a repeat of the benchmark without restarting Chrome. It
  # must be supported in benchmark-specific code.) This supports the smoke
  # test use case since we don't want to waste time with iterations in smoke
  # tests.
  group.add_option('--max-failures', default=None, type='int',
                   help='Maximum number of test failures before aborting '
                   'the run. Defaults to the number specified by the '
                   'PageTest.')
  group.add_option('--pause', dest='pause', default=None,
                   choices=_PAUSE_STAGES,
                   help='Pause for interaction at the specified stage. '
                   'Valid stages are %s.' % ', '.join(_PAUSE_STAGES))
  group.add_option('--suppress-gtest-report', action='store_true',
                   help='Suppress gtest style report of progress as stories '
                   'are being run.')
  group.add_option('--skip-typ-expectations-tags-validation',
                   action='store_true',
                   help='Suppress typ expectation tags validation errors.')
  parser.add_option_group(group)

  group = optparse.OptionGroup(parser, 'Web Page Replay options')
  group.add_option(
      '--use-live-sites',
      dest='use_live_sites', action='store_true',
      help='Run against live sites and ignore the Web Page Replay archives.')
  parser.add_option_group(group)

  parser.add_option('-p', '--print-only', dest='print_only',
                    choices=['stories', 'tags', 'both'], default=None)
  parser.add_option('-w', '--wait-for-cpu-temp',
                    dest='wait_for_cpu_temp', action='store_true',
                    default=False,
                    help='Introduces a wait between each story '
                    'until the device CPU has cooled down. If '
                    'not specified, this wait is disabled. '
                    'Device must be supported. ')


def ProcessCommandLineArgs(parser, args, environment=None):
  story_filter_module.StoryFilterFactory.ProcessCommandLineArgs(
      parser, args, environment)

  if args.pageset_repeat < 1:
    parser.error('--pageset-repeat must be a positive integer.')


@contextlib.contextmanager
def CaptureLogsAsArtifacts(results):
  with results.CreateArtifact('logs.txt') as log_file:
    with logging_util.CaptureLogs(log_file):
      yield


def _RunStoryAndProcessErrorIfNeeded(story, results, state, test):
  def ProcessError(exc, log_message):
    logging.exception(log_message)
    state.DumpStateUponStoryRunFailure(results)

    # Dump app crash, if present
    if exc:
      if isinstance(exc, exceptions.AppCrashException):
        minidump_path = exc.minidump_path
        if minidump_path:
          with results.CaptureArtifact('minidump.dmp') as path:
            shutil.move(minidump_path, path)

    # Note: calling Fail on the results object also normally causes the
    # progress_reporter to log it in the output.
    results.Fail('Exception raised running %s' % story.name)

  with CaptureLogsAsArtifacts(results):
    try:
      if isinstance(test, story_test.StoryTest):
        test.WillRunStory(state.platform)
      state.WillRunStory(story)

      if not state.CanRunStory(story):
        results.Skip(
            'Skipped because story is not supported '
            '(SharedState.CanRunStory() returns False).')
        return
      story.wpr_mode = state.wpr_mode
      state.RunStory(results)
      if isinstance(test, story_test.StoryTest):
        test.Measure(state.platform, results)
    except page_action.PageActionNotSupported as exc:
      results.Skip('Unsupported page action: %s' % exc)
    except (legacy_page_test.Failure, exceptions.TimeoutException,
            exceptions.LoginException, py_utils.TimeoutException) as exc:
      ProcessError(exc, log_message='Handleable error')
    except _UNHANDLEABLE_ERRORS as exc:
      ProcessError(exc, log_message=('Unhandleable error. '
                                     'Benchmark run will be interrupted'))
      raise
    except Exception as exc:  # pylint: disable=broad-except
      ProcessError(exc, log_message=('Possibly handleable error. '
                                     'Will try to restart shared state'))
      # The caller, RunStorySet, will catch this exception, destory and
      # create a new shared state.
      raise
    finally:
      has_existing_exception = (sys.exc_info() != (None, None, None))
      try:
        # We attempt to stop tracing and/or metric collecting before possibly
        # closing the browser. Closing the browser first and stopping tracing
        # later appeared to cause issues where subsequent browser instances
        # would not launch correctly on some devices (see: crbug.com/720317).
        # The following normally cause tracing and/or metric collecting to stop.
        if isinstance(test, story_test.StoryTest):
          test.DidRunStory(state.platform, results)
        else:
          test.DidRunPage(state.platform)
        # And the following normally causes the browser to be closed.
        state.DidRunStory(results)
      except Exception:  # pylint: disable=broad-except
        if not has_existing_exception:
          state.DumpStateUponStoryRunFailure(results)
          raise
        # Print current exception and propagate existing exception.
        exception_formatter.PrintFormattedException(
            msg='Exception raised when cleaning story run: ')


def _GetPossibleBrowser(finder_options):
  """Return a possible_browser with the given options."""
  possible_browser = browser_finder.FindBrowser(finder_options)
  if not possible_browser:
    raise browser_finder_exceptions.BrowserFinderException(
        'Cannot find browser of type %s. \n\nAvailable browsers:\n%s\n' % (
            finder_options.browser_options.browser_type,
            '\n'.join(browser_finder.GetAllAvailableBrowserTypes(
                finder_options))))

  finder_options.browser_options.browser_type = possible_browser.browser_type

  return possible_browser


def RunStorySet(test, story_set, finder_options, results, max_failures=None,
                max_num_values=sys.maxint):
  """Runs a test against a story_set with the given options.

  Stop execution for unexpected exceptions such as KeyboardInterrupt. Some
  other exceptions are handled and recorded before allowing the remaining
  stories to run.

  Args:
    test: Either a StoryTest or a LegacyPageTest instance.
    story_set: A StorySet instance with the set of stories to run.
    finder_options: The parsed command line options to customize the run.
    results: A PageTestResults object used to collect results and artifacts.
    max_failures: Max number of story run failures allowed before aborting
      the entire story run. It's overriden by finder_options.max_failures
      if given.
    expectations: Benchmark expectations used to determine disabled stories.
    max_num_values: Max number of legacy values allowed before aborting the
      story run.
  """
  stories = story_set.stories
  for s in stories:
    ValidateStory(s)

  # TODO(crbug.com/1013630): A possible_browser object was already created in
  # Run() method, so instead of creating a new one here we should be
  # using that one.
  possible_browser = _GetPossibleBrowser(finder_options)
  platform_tags = possible_browser.GetTypExpectationsTags()
  logging.info('The following expectations condition tags were generated %s',
               str(platform_tags))
  abridged_story_set_tag = story_set.GetAbridgedStorySetTagFilter()
  story_filter = story_filter_module.StoryFilterFactory.BuildStoryFilter(
      results.benchmark_name, platform_tags, abridged_story_set_tag)
  stories = story_filter.FilterStories(stories)
  wpr_archive_info = story_set.wpr_archive_info
  # Sort the stories based on the archive name, to minimize how often the
  # network replay-server needs to be restarted.
  if wpr_archive_info:
    stories = sorted(stories, key=wpr_archive_info.WprFilePathForStory)

  if finder_options.print_only:
    if finder_options.print_only == 'tags':
      tags = set(itertools.chain.from_iterable(s.tags for s in stories))
      print 'List of tags:\n%s' % '\n'.join(tags)
      return
    include_tags = finder_options.print_only == 'both'
    if include_tags:
      format_string = '  %%-%ds %%s' % max(len(s.name) for s in stories)
    else:
      format_string = '%s%s'
    for s in stories:
      print format_string % (s.name, ','.join(s.tags) if include_tags else '')
    return

  if (not finder_options.use_live_sites and
      finder_options.browser_options.wpr_mode != wpr_modes.WPR_RECORD):
    # Get the serving dirs of the filtered stories.
    # TODO(crbug.com/883798): removing story_set._serving_dirs
    serving_dirs = story_set._serving_dirs.copy()
    for story in stories:
      if story.serving_dir:
        serving_dirs.add(story.serving_dir)

    if story_set.bucket:
      for directory in serving_dirs:
        cloud_storage.GetFilesInDirectoryIfChanged(directory,
                                                   story_set.bucket)
    if story_set.archive_data_file and not _UpdateAndCheckArchives(
        story_set.archive_data_file, wpr_archive_info, stories):
      return

  if not stories:
    return

  # Effective max failures gives priority to command-line flag value.
  effective_max_failures = finder_options.max_failures
  if effective_max_failures is None:
    effective_max_failures = max_failures

  state = None
  device_info_diags = {}
  # TODO(crbug.com/866458): unwind the nested blocks
  # pylint: disable=too-many-nested-blocks
  try:
    pageset_repeat = finder_options.pageset_repeat
    for storyset_repeat_counter in xrange(pageset_repeat):
      for story in stories:
        if not state:
          # Construct shared state by using a copy of finder_options. Shared
          # state may update the finder_options. If we tear down the shared
          # state after this story run, we want to construct the shared
          # state for the next story from the original finder_options.
          state = story_set.shared_state_class(
              test, finder_options.Copy(), story_set, possible_browser)

        results.WillRunPage(story, storyset_repeat_counter)

        skip_reason = story_filter.ShouldSkip(story)
        if skip_reason:
          results.Skip(skip_reason)
          results.DidRunPage(story)
          continue

        if results.benchmark_interrupted:
          results.Skip(results.benchmark_interruption, is_expected=False)
          results.DidRunPage(story)
          continue

        try:
          if state.platform:
            state.platform.WaitForBatteryTemperature(35)
            if finder_options.wait_for_cpu_temp:
              state.platform.WaitForCpuTemperature(38.0)
            _WaitForThermalThrottlingIfNeeded(state.platform)
          _RunStoryAndProcessErrorIfNeeded(story, results, state, test)

          num_values = sum(1 for _ in results.IterAllLegacyValues())
          # TODO(#4259): Convert this to an exception-based failure
          if num_values > max_num_values:
            msg = 'Too many values: %d > %d' % (num_values, max_num_values)
            logging.error(msg)
            results.Fail(msg)

          device_info_diags = _MakeDeviceInfoDiagnostics(state)
        except _UNHANDLEABLE_ERRORS as exc:
          interruption = (
              'Benchmark execution interrupted by a fatal exception: %r' % exc)
          results.InterruptBenchmark(interruption)
          exception_formatter.PrintFormattedException()
        except Exception:  # pylint: disable=broad-except
          logging.exception('Exception raised during story run.')
          results.Fail(sys.exc_info())
          # For all other errors, try to give the rest of stories a chance
          # to run by tearing down the state and creating a new state instance
          # in the next iteration.
          try:
            # If TearDownState raises, do not catch the exception.
            # (The Error was saved as a failure value.)
            state.TearDownState()
          except Exception as exc:  # pylint: disable=broad-except
            interruption = (
                'Benchmark execution interrupted by a fatal exception: %r' %
                exc)
            results.InterruptBenchmark(interruption)
            exception_formatter.PrintFormattedException()
          finally:
            # Later finally-blocks use state, so ensure it is cleared.
            state = None
        finally:
          if state and state.platform:
            _CheckThermalThrottling(state.platform)
          results.DidRunPage(story)
        if (effective_max_failures is not None and
            results.num_failed > effective_max_failures):
          interruption = (
              'Too many stories failed. Aborting the rest of the stories.')
          results.InterruptBenchmark(interruption)
  finally:
    results_processor.ComputeTimelineBasedMetrics(results)
    results.PopulateHistogramSet()
    results.AddSharedDiagnostics(**device_info_diags)

    if state:
      has_existing_exception = sys.exc_info() != (None, None, None)
      try:
        state.TearDownState()
      except Exception: # pylint: disable=broad-except
        if not has_existing_exception:
          raise
        # Print current exception and propagate existing exception.
        exception_formatter.PrintFormattedException(
            msg='Exception from TearDownState:')


def ValidateStory(story):
  if len(story.name) > 180:
    raise ValueError(
        'User story has name exceeding 180 characters: %s' %
        story.name)


def _ShouldRunBenchmark(benchmark, possible_browser, finder_options):
  if finder_options.print_only:
    return True  # Should always run on print-only mode.
  if benchmark.CanRunOnPlatform(possible_browser.platform, finder_options):
    return True
  print ('Benchmark "%s" is not supported on the current platform. If this '
         "is in error please add it to the benchmark's SUPPORTED_PLATFORMS."
         % benchmark.Name())
  return False


def RunBenchmark(benchmark, finder_options):
  """Run this test with the given options.

  Returns:
    -1 if the benchmark was skipped,
    0 for success
    1 if there was a failure
    2 if there was an uncaught exception.
  """
  benchmark.CustomizeOptions(finder_options)
  with results_options.CreateResults(
      finder_options,
      benchmark_name=benchmark.Name(),
      benchmark_description=benchmark.Description(),
      report_progress=not finder_options.suppress_gtest_report) as results:

    possible_browser = browser_finder.FindBrowser(finder_options)
    if not possible_browser:
      print ('No browser of type "%s" found for running benchmark "%s".' % (
          finder_options.browser_options.browser_type, benchmark.Name()))
      return -1
    if not _ShouldRunBenchmark(benchmark, possible_browser, finder_options):
      return -1

    test = benchmark.CreatePageTest(finder_options)
    test.__name__ = benchmark.__class__.__name__

    story_set = benchmark.CreateStorySet(finder_options)

    if isinstance(test, legacy_page_test.LegacyPageTest):
      if any(not isinstance(p, page.Page) for p in story_set.stories):
        raise Exception(
            'PageTest must be used with StorySet containing only '
            'telemetry.page.Page stories.')
    try:
      RunStorySet(
          test, story_set, finder_options, results, benchmark.max_failures,
          max_num_values=benchmark.MAX_NUM_VALUES)
      if results.benchmark_interrupted:
        return_code = 2
      elif results.had_failures:
        return_code = 1
      elif results.had_successes:
        return_code = 0
      else:
        return_code = -1  # All stories were skipped.
    except Exception as exc: # pylint: disable=broad-except
      interruption = 'Benchmark execution interrupted: %r' % exc
      results.InterruptBenchmark(interruption)
      exception_formatter.PrintFormattedException()
      return_code = 2

    # TODO(crbug.com/981349): merge two calls to AddSharedDiagnostics
    # (see RunStorySet() method for the second one).
    results.AddSharedDiagnostics(
        owners=benchmark.GetOwners(),
        bug_components=benchmark.GetBugComponents(),
        documentation_urls=benchmark.GetDocumentationLinks(),
    )

    if finder_options.upload_results:
      results_processor.UploadArtifactsToCloud(results)
  return return_code

def _UpdateAndCheckArchives(archive_data_file, wpr_archive_info,
                            filtered_stories):
  """Verifies that all stories are local or have WPR archives.

  Logs warnings and returns False if any are missing.
  """
  # Report any problems with the entire story set.
  story_names = [s.name for s in filtered_stories if not s.is_local]
  if story_names:
    if not archive_data_file:
      logging.error('The story set is missing an "archive_data_file" '
                    'property.\nTo run from live sites pass the flag '
                    '--use-live-sites.\nTo create an archive file add an '
                    'archive_data_file property to the story set and then '
                    'run record_wpr.')
      raise ArchiveError('No archive data file.')
    if not wpr_archive_info:
      logging.error('The archive info file is missing.\n'
                    'To fix this, either add svn-internal to your '
                    '.gclient using http://goto/read-src-internal, '
                    'or create a new archive using record_wpr.')
      raise ArchiveError('No archive info file.')
    wpr_archive_info.DownloadArchivesIfNeeded(story_names=story_names)

  # Report any problems with individual story.
  stories_missing_archive_path = []
  stories_missing_archive_data = []
  for story in filtered_stories:
    if not story.is_local:
      archive_path = wpr_archive_info.WprFilePathForStory(story)
      if not archive_path:
        stories_missing_archive_path.append(story)
      elif not os.path.isfile(archive_path):
        stories_missing_archive_data.append(story)
  if stories_missing_archive_path:
    logging.error(
        'The story set archives for some stories do not exist.\n'
        'To fix this, record those stories using record_wpr.\n'
        'To ignore this warning and run against live sites, '
        'pass the flag --use-live-sites.')
    logging.error(
        'stories without archives: %s',
        ', '.join(story.name
                  for story in stories_missing_archive_path))
  if stories_missing_archive_data:
    logging.error(
        'The story set archives for some stories are missing.\n'
        'Someone forgot to check them in, uploaded them to the '
        'wrong cloud storage bucket, or they were deleted.\n'
        'To fix this, record those stories using record_wpr.\n'
        'To ignore this warning and run against live sites, '
        'pass the flag --use-live-sites.')
    logging.error(
        'stories missing archives: %s',
        ', '.join(story.name
                  for story in stories_missing_archive_data))
  if stories_missing_archive_path or stories_missing_archive_data:
    raise ArchiveError('Archive file is missing stories.')
  # Only run valid stories if no problems with the story set or
  # individual stories.
  return True


def _WaitForThermalThrottlingIfNeeded(platform):
  if not platform.CanMonitorThermalThrottling():
    return
  thermal_throttling_retry = 0
  while (platform.IsThermallyThrottled() and
         thermal_throttling_retry < 3):
    logging.warning('Thermally throttled, waiting (%d)...',
                    thermal_throttling_retry)
    thermal_throttling_retry += 1
    time.sleep(thermal_throttling_retry * 2)

  if thermal_throttling_retry and platform.IsThermallyThrottled():
    logging.warning('Device is thermally throttled before running '
                    'performance tests, results will vary.')


def _CheckThermalThrottling(platform):
  if not platform.CanMonitorThermalThrottling():
    return
  if platform.HasBeenThermallyThrottled():
    logging.warning('Device has been thermally throttled during '
                    'performance tests, results will vary.')

def _MakeDeviceInfoDiagnostics(state):
  if not state or not state.platform:
    return {}

  # This used to include data for reserved_infos.MEMORY_AMOUNTS, but it was
  # found that platform.GetSystemTotalPhysicalMemory() does not give
  # consistent results. See crbug.com/854676 for details.
  return {
      'architecture': state.platform.GetArchName(),
      'device_id': state.platform.GetDeviceId(),
      'os_name': state.platform.GetOSName(),
      'os_version': state.platform.GetOSVersionName(),
  }
