# VueGraf Change History

## v2.0.0 [2020-12-18, ai7]

todo:
- yaml config file
- auth token file
- passwords

- Prefix single digit channel number with a `0` in channel name, so they
  will be ordered correctly in Grafana legends, etc.
  - From `1, 11, 12, .., 16, 2, 3, 4, 5 ...` -> `01, 02, 03, ... 16`.

- Align start/end timestamp to **seconds** (drop microseconds). This ensures the
  correct number of data points for various pulling durations (ie, exactly 1
  data point for 1s duration, 60 data points for 60s duration, etc).
  - This also allow us to use the end time directly as start time in the
    next round (rather than having to +1s with microsecond timestamps).

- Added improvements to address the occasional missing data points as seen
  in grafana plots, either due to server errors, or incomplete data pulls.
  - On failure, do not advance the start time, so we'll attempt to
    re-collect data points for the failed duration in the next round.
  - Detect incomplete data pulls, and retry on next round. This can happen
    when there are no errors/exceptions, but we did not retrieve all of the
    data points for our specified duration. This could be due to network
    delays / errors, or too short of a lag time.
  - Using a shorter delay (20s) in retrying mode. 20s is long enough and
    should not cause any retry-storm to Emporia service.
  - Abort retry (advance start time) when we have 3 consecutive retry
    failures due to incomplete data.

- Added logging capabilities. This allows us to more easily troubleshoot and
  review history / action / errors.
  - Rotating logs at certain file size, with specific number of backups, as
    to not filling up disk space.
  - using localtime (rather than utc) for logging timestamp. This allow us
    to more easily correlate missing data in grafana plots to log entries.
  - added various log messages so we can more clearily follow program and
    run progress.

- Better command line parsing. Command line options with defaults to speficy
  **config file**, **log file**, **lag time**, and **pull intervals**.
  Allowing us to easily experiment with various settings.
  - More command line options can be easily added using decorators.

- Enhanced **colorized** console output to provide better *at-glance*
  information for much longer period of time.
  - Various program settings/progress on startup.
  - Detailed output of discovered device and channels:
    - name, guid, channel number, model, firmware.
  - For each data pull, compact output for `[pull #, duration, channels,
    min/max data points for channels, total data points collected]`.
    - Different color when channel min/max is not the same. Useful for
      tuning lag time.
  - New separating header when we cross the hour mark.
    - Different color when errors are encountered. Useful for gathering how
      much failures we have in each hour.
  - Output some program statistics on exit.

- Misc refactoring changes
  - Added python type hints to various functions to allow better IDE
    auto-complete and type checking.
  - Fixed various minor PEP8 issues.
  - Added Stat object to keep track of various statistics for each round.
    Output stat at end of program on exit.
  - Moved main script under main() function for better modularity.
  - Added comments for interesting/non-obvious parts.

- New dependencies:
  - `click`: easy command line parsing and colorized console output
  - `yaml`: more natural config file with comments
