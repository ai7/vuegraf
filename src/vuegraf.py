#!/usr/bin/env python3

import os
import platform
import datetime
import json
import signal
import sys
import time
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
FAILURE_SECS = 10         # shorter delay when we encounter a failure
LAG_SECS = 5

logger = logging.getLogger()


def setup_logging():
    """Setting up a rotating log handler, 5MB size and 2 backups"""
    logfile = os.path.join(tempfile.gettempdir(), LOG_FILE)
    print("log file:", logfile)

    log_formatter = logging.Formatter('%(asctime)s| %(levelname)-8s| %(message)s')
    my_handler = RotatingFileHandler(logfile, mode='a', maxBytes=5 * 1024 * 1024,
                                     backupCount=2, encoding=None, delay=0)
    my_handler.setFormatter(log_formatter)
    my_handler.setLevel(logging.INFO)

    logger.setLevel(logging.INFO)
    logger.addHandler(my_handler)

    logger.info('***** %s started *****', 'vuegraf')
    logger.info(get_system_info())


def get_system_info():
    # type: () -> str
    name = platform.system()
    ver = platform.release()
    if name == 'Darwin':
        name = 'MacOS'
        ver = platform.mac_ver()[0]
    elif name == 'Linux':
        dist = platform.linux_distribution()
        name = dist[0]
        ver = dist[1]
    # host: MacOS 10.14.6, x86_64, python 3.7.9 [2020-12-15 13:49:23 PST]
    info_str = '{}: {} {}, {}, python {}'.format(
        platform.node(), name, ver, platform.machine(), sys.version.split()[0])
    return info_str


if len(sys.argv) != 2:
    print('Usage: python {} <config-file>'.format(sys.argv[0]))
    sys.exit(1)

setup_logging()

configFilename = sys.argv[1]
config = {}
with open(configFilename) as configFile:
    logger.info('loading config file %s', configFilename)
    config = json.load(configFile)

# Only authenticate to ingress if 'user' entry was provided in config
if 'user' in config['influxDb']:
    logger.info('connecting to influxDb with user/password')
    influx = InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'],
                            username=config['influxDb']['user'], password=config['influxDb']['pass'],
                            database=config['influxDb']['database'])
else:
    logger.info('connecting to influxDb')
    influx = InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'],
                            database=config['influxDb']['database'])

logger.info('creating database: %s', config['influxDb']['database'])
influx.create_database(config['influxDb']['database'])

running = True


# flush=True helps when running in a container without a tty attached
# (alternatively, "python -u" or PYTHONUNBUFFERED will help here)
def log(level, msg):
    # type: (str, str) -> None
    # now = datetime.datetime.now()
    # print('{} | {} | {}'.format(now, level.ljust(5), msg), flush=True)
    print(msg, flush=True)


def info(msg):
    # type: (str) -> None
    log("INFO", msg)


def error(msg):
    # type: (str) -> None
    log("ERROR", msg)


def handleExit(signum, frame):
    global running
    error('Caught exit signal')
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
    for device in devices:
        device = account['vue'].populate_device_properties(device)
        logger.info("device %s: id=%s, model=%s, firmware=%s", device.device_name,
                    device.device_gid, device.model, device.firmware)
        deviceIdMap[device.device_gid] = device
        for chan in device.channels:
            key = "{}-{}".format(device.device_gid, chan.channel_num)
            if chan.name is None and chan.channel_num == DEVICE_CHANNEL:
                chan.name = device.device_name
            else:
                logger.info("  channel %s: %s", chan.channel_num, chan.name)
            channelIdMap[key] = chan
            info("Discovered new channel: {} ({})".format(chan.name, chan.channel_num))


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


signal.signal(signal.SIGINT, handleExit)
signal.signal(signal.SIGHUP, handleExit)

pauseEvent = Event()

if config['influxDb']['reset']:
    info('Resetting database')
    influx.delete_series(measurement='energy_usage')

uptime_start = datetime.datetime.now()  # to track total program uptime
success_run = True  # on failure, do not advance start
run_count = 0  # total number of iterations
hour_count = 0  # number of hours elapsed
last_start = None  # to track when the hour changes
count_in_hour = 0

while running:
    run_count += 1
    count_in_hour += 1
    run_start_time = datetime.datetime.now()
    logger.info('starting run --> %d <-- [uptime: %s]', run_count,  run_start_time - uptime_start)

    # todo: test more than one account to ensure console output looks reasonable
    for account in config["accounts"]:
        # compute polling ending time, which LAG_SECS before current time
        tmpEndingTime = datetime.datetime.utcnow() - datetime.timedelta(seconds=LAG_SECS)
        # align timestamp to the second, this results in the correct number of points for
        # the initial poll, 60, instead of 61 as with non zero microseconds.
        # it also allows us to just use end time as the next start time in next round.
        tmpEndingTime = tmpEndingTime.replace(microsecond=0)

        if 'vue' not in account:  # first iteration, create objects
            logger.info("logging into Emporia for account: %s", account['name'])
            account['vue'] = PyEmVue()
            account['vue'].login(username=account['email'], password=account['password'])
            info('Login completed')

            logger.info("populating devices")
            populateDevices(account)

            account['end'] = tmpEndingTime

            start = account['end'] - datetime.timedelta(seconds=INTERVAL_SECS)  # start with 60s prior

            result = influx.query('select last(usage), time from energy_usage where account_name = \'{}\''
                                  .format(account['name']))
            if len(result) > 0:
                timeStr = next(result.get_points())['time']
                tmpStartingTime = datetime.datetime.strptime(timeStr, '%Y-%m-%dT%H:%M:%SZ')
                if tmpStartingTime > start:  # if already have data after our computed start
                    start = tmpStartingTime  # use the last data timestamp as new start
        else:
            if success_run:
                # start = account['end'] + datetime.timedelta(seconds=1)
                start = account['end']  # use end time as next start
                assert(tmpEndingTime > start)
            else:
                # if not successful, continue to use the same start time,
                # so the new poll includes failed time slot.
                # todo: if failure exceeds 1h, we probably need to reset start
                pass
            account['end'] = tmpEndingTime

        logger.info('query interval: %s [%s - %s]', account['end'] - start, start, account['end'])

        # if starting in a new hour, output a visible header
        if last_start is None or start.hour > last_start.hour:
            hour_count += 1
            count_in_hour = 1  # reset counter for the hour back to 1
            if last_start:
                click.echo('')  # new line if have previous data
            click.secho('[{}] {}'.format(hour_count, run_start_time), bold=True, fg='yellow', nl=False)
            click.echo(' -> [pull #, duration, min-max channel points, total data points]')

        click.echo('[#{} '.format(count_in_hour), nl=False)
        click.secho('{}'.format(account['end'] - start), fg='green', bold=True, nl=False)

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
                    channel_min = min(channel_min, index)
                    channel_max = max(channel_max, index)

            logger.info('writing %d data points to database.', len(usageDataPoints))
            influx.write_points(usageDataPoints)

            # info('Submitted datapoints to database; account="{}"; points={}'.format(account['name'],
            #                                                                         len(usageDataPoints)))
            click.echo(' {}-{}'.format(channel_min, channel_max), nl=False)
            click.secho(' {}'.format(len(usageDataPoints)), bold=True, nl=False)
            click.echo(']', nl=False)

            logger.info('run %d successful.', run_count)
            success_run = True
        except:
            success_run = False
            # error('Failed to record new usage data: {}'.format(sys.exc_info()))
            logger.info('run %d failed: %s', run_count, sys.exc_info())
            click.echo('ERROR {}]'.format(datetime.datetime.now()), bold=True, color='red')

    last_start = start
    sleep_seconds = INTERVAL_SECS if success_run else FAILURE_SECS
    logger.info('sleeping for %d seconds', sleep_seconds)
    pauseEvent.wait(sleep_seconds)

# todo: output statistics
info('Finished')
