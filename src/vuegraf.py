#!/usr/bin/env python3

import os
import datetime
import json
import signal
import tempfile
import logging
from logging.handlers import RotatingFileHandler
from threading import Event

import click
from influxdb import InfluxDBClient
from pyemvue import PyEmVue
from pyemvue.device import VuewDeviceChannelUsage
from pyemvue.enums import Scale, Unit, TotalTimeFrame, TotalUnit


LOG_FILE = 'vuegraf.log'  # log file created in temp
DEVICE_CHANNEL = '1,2,3'  # main/parent device

INTERVAL_SECS = 60        # delay between each pulling of usage data from emporia
FAILURE_SECS = 20         # shorter delay when we encounter a failure/failed pull

# how many channel data points are allowed to miss for the pull to be
# considered successful.
SUCCESS_THRESHOLD = 5
SUCCESS_THRESHOLD2 = 10  # min should be this close to max channel points

# Sufficient lag time will ensure that we always get the desired number of
# data points for every channel during each pull, which should be one for
# every second. Too small of a lag time will cause some or all channels to
# have incomplete data for the pull duration.
LAG_SECS = 20

logger = logging.getLogger()
pauseEvent = Event()
running = True


def console_info(message, value, color):
    """print message, then value with specified color in bold"""
    click.echo(message, nl=False)
    click.secho(str(value), fg=color, bold=True)


def setup_logging(logfile):
    # type: (str) -> None
    """Setting up a rotating log handler, 5MB size and 2 backups"""
    if not logfile:
        logfile = os.path.join(tempfile.gettempdir(), LOG_FILE)
    console_info('log file: ', logfile, 'white')

    log_formatter = logging.Formatter('%(asctime)s| %(levelname)-8s| %(message)s')
    my_handler = RotatingFileHandler(logfile, mode='a', maxBytes=5 * 1024 * 1024,
                                     backupCount=2, encoding=None, delay=0)
    my_handler.setFormatter(log_formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(my_handler)

    logger.info('***** %s started *****', 'vuegraf')


def handleExit(signum, frame):
    global running
    logger.error('Caught exit signal')
    running = False
    pauseEvent.set()


def populateDevices(account):
    # type: (dict) -> None
    deviceIdMap = {}
    account['deviceIdMap'] = deviceIdMap
    channelIdMap = {}
    account['channelIdMap'] = channelIdMap

    devices = account['vue'].get_devices()
    logger.info("found %d devices", len(devices))
    click.secho('[{}]'.format(len(devices)), fg='green', bold=True)

    device_count = 0
    for device in devices:
        device_count += 1
        device = account['vue'].populate_device_properties(device)
        deviceIdMap[device.device_gid] = device

        # output some info to log/screen
        metadata = 'id={}, model={}, firmware={}'.format(device.device_gid, device.model, device.firmware)
        logger.info("device %s: %s", device.device_name, metadata)
        click.echo('[{}] '.format(device_count), nl=False)
        click.secho(device.device_name, fg='white', bold=True, nl=False)
        click.echo(' ({})'.format(metadata))

        for chan in device.channels:
            key = "{}-{}".format(device.device_gid, chan.channel_num)
            if chan.name is None and chan.channel_num == DEVICE_CHANNEL:
                chan.name = device.device_name
            else:
                logger.info("  channel %s: %s", chan.channel_num, chan.name)
                console_info('  channel {:02d}: '.format(int(chan.channel_num)), chan.name, 'white')
            channelIdMap[key] = chan


def lookupDeviceName(account, device_gid):
    # type: (dict, int) -> str
    if device_gid not in account['deviceIdMap']:
        populateDevices(account)

    deviceName = "{}".format(device_gid)
    if device_gid in account['deviceIdMap']:
        deviceName = account['deviceIdMap'][device_gid].device_name
    return deviceName


def lookupChannelName(account, chan):
    # type: (dict, VuewDeviceChannelUsage) -> str
    if chan.device_gid not in account['deviceIdMap']:
        populateDevices(account)

    deviceName = lookupDeviceName(account, chan.device_gid)
    if chan.channel_num.isdigit():  # all digit, format to XX with leading 0 for better sorting
        name = "{}-{:02d}".format(deviceName, int(chan.channel_num))
    else:  # otherwise, ie '1,2,3', regular formatting
        name = "{}-{}".format(deviceName, chan.channel_num)
    if 'devices' in account:
        for device in account['devices']:
            if 'name' in device and device['name'] == deviceName:
                try:
                    num = int(chan.channel_num)
                    if 'channels' in device and len(device['channels']) >= num:
                        name = device['channels'][num - 1]
                except:
                    name = deviceName
    return name


def check_failed_pull(min_points, max_points, pull_duration):
    # type: (int, int, datetime.timedelta) -> bool
    """
    Check if this is a successful run by looking at min/max data points collected
    for the channel, and how it compares with the pull duration in seconds.
    """
    # Here we use a simple check to see if the maximum channel points we get
    # is close enough to our duration, and min is not too far behind.
    #
    # Usually when we see blanks in our graph, we received far fewer points
    # than our duration (which is typically ~1m). So this check will catch
    # those cases. Using discrete points/seconds is easier to understand
    # than using a percentile.
    #
    # It is worth noting that during testing, the emporia smart plugs seems
    # to occasionally miss a few data points, while the vue does have all
    # the data points. Perhaps the smart plugs have a slightly larger update
    # interval compare to vue (the Emporia app shows 5 seconds updates vs 1s
    # with the vue).
    missing_points = pull_duration.seconds - max_points
    if missing_points > SUCCESS_THRESHOLD:
        logger.warning('failed run, missing {} data points'.format(missing_points))
        return True
    # also check the minimum to make sure it is not too much away from max
    delta_points = max_points - min_points
    if delta_points > SUCCESS_THRESHOLD2:
        logger.warning('failed run, min/max delta too big: {}'.format(delta_points))
        return True
    return False


class Stat:
    """keeps track of run statistics"""
    def __init__(self):
        self.uptime_start = datetime.datetime.now()  # to track total program uptime
        self.failed_run = 0  # on failure, do not advance start, if 3 consecutive failures, abort recovery
        self.run_count = 0  # total number of iterations
        self.hour_count = 0  # number of hours elapsed
        self.last_start = None  # to track when the hour changes
        self.count_in_hour = 0  # pull number within the hour
        self.run_start_time = None  # type: datetime
        self.total_failed_run = 0  # total number of failed runs
        self.total_data_points = 0  # total number of data points gathered

    def new_run(self):
        self.run_count += 1  # increment total number of runs
        self.count_in_hour += 1  # increment runs within the hour
        self.run_start_time = datetime.datetime.now()  # starting time for this round

    def exit_print(self):
        exit_time = datetime.datetime.now()
        print('\n\ntotal runtime: {} [{} - {}]'.format(exit_time - self.uptime_start, self.uptime_start, exit_time))
        print('total data pulls: {:,}, failed {:,}'.format(self.run_count, self.total_failed_run))
        print('total data points recorded: {:,}'.format(self.total_data_points))


@click.command()
@click.option('-c', '--config', 'configfile', help='config file [vuegraf.json]',
              default='vuegraf.json', type=click.Path(exists=True))
@click.option('-l', '--log', 'logfile', metavar='PATH', help='log file [$TMP/vuegraf.log]')
@click.option('--lag', metavar='SECONDS', default=LAG_SECS, help='lag time [20s]')
@click.option('--interval', metavar='SECONDS', default=INTERVAL_SECS, help='pull/sleep interval [60s]')
def main(configfile, logfile, lag, interval):
    global running

    setup_logging(logfile)
    console_info('config file: ', configfile, 'green')
    console_info('pulling interval: ', interval, 'green')
    console_info('lag seconds: ', lag, 'green')

    config = {}
    with open(configfile) as configFile:
        logger.info('loading config file %s', configfile)
        config = json.load(configFile)

    # Only authenticate to ingress if 'user' entry was provided in config
    console_info('logging into influxDB: ', config['influxDb']['host'], 'green')
    if 'user' in config['influxDb']:
        logger.info('connecting to influxDB with user/password')
        influx = InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'],
                                username=config['influxDb']['user'], password=config['influxDb']['pass'],
                                database=config['influxDb']['database'])
    else:
        logger.info('connecting to influxDb')
        influx = InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'],
                                database=config['influxDb']['database'])

    console_info('opening database: ', config['influxDb']['database'], 'yellow')
    logger.info('creating database: %s', config['influxDb']['database'])
    influx.create_database(config['influxDb']['database'])

    signal.signal(signal.SIGINT, handleExit)
    signal.signal(signal.SIGHUP, handleExit)

    if config['influxDb']['reset']:
        click.echo('Resetting database')
        influx.delete_series(measurement='energy_usage')

    S = Stat()

    while running:
        S.new_run()
        logger.info('starting run --> %d <-- [uptime: %s]', S.run_count,  S.run_start_time - S.uptime_start)

        # todo: test more than one account to ensure console output looks reasonable
        for account in config["accounts"]:
            # compute polling ending time, which LAG_SECS before current time
            tmpEndingTime = datetime.datetime.utcnow() - datetime.timedelta(seconds=lag)
            # align timestamp to the second, this results in the correct number of points for
            # the initial poll, 60, instead of 61 as with non zero microseconds.
            # it also allows us to just use end time as the next start time in next round.
            tmpEndingTime = tmpEndingTime.replace(microsecond=0)

            if 'vue' not in account:  # first iteration, create objects
                console_info('logging into Emporia for: ', account['name'], 'green')
                logger.info("logging into Emporia for account: %s", account['name'])
                account['vue'] = PyEmVue()
                account['vue'].login(username=account['email'], password=account['password'])

                click.echo('discovering devices ... ', nl=False)
                logger.info("populating devices")
                populateDevices(account)

                account['end'] = tmpEndingTime

                start = account['end'] - datetime.timedelta(seconds=interval)  # start with 60s prior

                result = influx.query('select last(usage), time from energy_usage where account_name = \'{}\''
                                      .format(account['name']))
                if len(result) > 0:
                    # timestamp from db may have microseconds like '2020-12-16T06:57:28.985109Z'
                    # from previous runs before we aligned time to seconds. So we want to chop
                    # off the fractional part here and add the Z back.
                    timeStr = next(result.get_points())['time'][:19] + 'Z'
                    tmpStartingTime = datetime.datetime.strptime(timeStr, '%Y-%m-%dT%H:%M:%SZ')
                    if tmpStartingTime > start:  # if already have data after our computed start
                        start = tmpStartingTime  # use the last data timestamp as new start
            else:
                if not S.failed_run:
                    # start = account['end'] + datetime.timedelta(seconds=1)
                    start = account['end']  # use end time as next start
                    assert(tmpEndingTime > start)
                else:
                    # if not successful, continue to use the same start time,
                    # so the new pull includes failed time slot.
                    # todo: if failure exceeds 1h, we probably need to reset start
                    pass
                account['end'] = tmpEndingTime

            run_duration = account['end'] - start
            logger.info('query interval: %s [%s - %s]', run_duration, start, account['end'])

            # if starting in a new hour, output a more visible header in console
            if S.last_start is None or start.hour != S.last_start.hour:
                S.hour_count += 1
                S.count_in_hour = 1  # reset counter for the hour back to 1
                if S.last_start:
                    click.echo('')  # new line if have previous data
                click.secho('[{}] {}'.format(S.hour_count, S.run_start_time), bold=True, fg='yellow')

            click.echo('[#{} '.format(S.count_in_hour), nl=False)
            click.secho('{}'.format(run_duration), fg='green', bold=True, nl=False)

            try:
                # gets the last minute total usage for each channel under account
                logger.info('getting usage for account')
                channels = account['vue'].get_recent_usage(Scale.MINUTE.value)
                logger.info('get_recent_usage() returned %d channels', len(channels))

                usageDataPoints = []
                device = None
                # keep track of the minimum and maximum number of data points collected
                # for the channels. These should more/less corresponds to the duration in seconds.
                channel_max = 0
                channel_min = 65536  # some large number
                data_channels = 0  # how many channels have some data
                for chan in channels:
                    chanName = lookupChannelName(account, chan)

                    # get the actual usage for this channel over the time interval
                    # usage is simply a list of floats, each representing each second
                    usage = account['vue'].get_usage_over_time(chan, start, account['end'])
                    index = 0
                    for watts in usage:
                        if watts is not None:
                            dataPoint = {
                                "measurement": "energy_usage",
                                "tags": {
                                    "account_name": account['name'],
                                    "device_name": chanName,
                                },
                                "fields": {
                                    "usage": watts,
                                },
                                "time": start + datetime.timedelta(seconds=index)
                            }
                            index = index + 1
                            usageDataPoints.append(dataPoint)
                    logger.info('%s: collected %d data points', chanName, index)

                    # update min/max points for channels
                    if index:  # some channels have all None, so we ignore those
                        data_channels += 1
                        channel_min = min(channel_min, index)
                        channel_max = max(channel_max, index)

                if data_channels:
                    click.echo('/{}c'.format(data_channels), nl=False)

                # check if the data pull is successful or not.
                if check_failed_pull(channel_min, channel_max, run_duration):
                    S.failed_run += 1
                    S.total_failed_run += 1
                    click.secho(' {}-{} {}'.format(channel_min, channel_max, len(usageDataPoints)),
                                fg='red', bold=True, nl=False)
                    click.echo(']', nl=False)
                    if S.failed_run > 3:  # if we failed 3 times in a row, abort recovery and continue
                        logger.error('3 consecutive incomplete retry, aborting retry')
                        S.failed_run = 0  # revert to success, so we wrote whatever we had
                    else:
                        logger.info('run %d failed, not enough data points')
                        continue
                else:
                    if channel_min == channel_max:  # if same, just output one number
                        click.echo(' {}'.format(channel_min), nl=False)
                    else:
                        # when the lag time is configured correctly, most pulls
                        # should have the same number of points for all
                        # channels. If not, we highlight this anomaly.
                        click.secho(' {}-{}'.format(channel_min, channel_max), fg='cyan', bold=True, nl=False)
                    click.secho(' {}'.format(len(usageDataPoints)), fg='white', bold=True, nl=False)
                    click.echo(']', nl=False)

                logger.info('writing %d data points to database.', len(usageDataPoints))
                influx.write_points(usageDataPoints)
                S.total_data_points += len(usageDataPoints)

                # info('Submitted datapoints to database; account="{}"; points={}'.format(account['name'],
                #                                                                         len(usageDataPoints)))
                logger.info('run %d successful.', S.run_count)
                S.failed_run = 0
            except Exception as e:
                # This is likely some kind of HTTP exception. We'll just
                # keep trying every 20s until the service is restored.
                #
                # We do need to reset the failure counter, since we want a
                # full pull after service comes back (from when failure
                # started, and also retry IF that pull didn't yield complete
                # data). Not resetting the counter would result in no-retry
                # if the first pull after service restore did not yield all
                # the data points, which does happen occasionally.
                S.failed_run = 1
                S.total_failed_run += 1
                logger.error('run %d exception: %s', S.run_count, e)
                click.secho(' Exception @ {}'.format(
                    datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')), bold=True, fg='red', nl=False)
                click.echo(']', nl=False)

        S.last_start = start
        sleep_seconds = FAILURE_SECS if S.failed_run else interval
        logger.info('sleeping for %d seconds', sleep_seconds)
        pauseEvent.wait(sleep_seconds)

    # all done, print some statistics and then exit
    S.exit_print()


if __name__ == '__main__':
    main()
