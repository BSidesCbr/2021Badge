#!/usr/bin/env python3
import argparse
import json
import logging
import os
import pathlib
import pprint
import subprocess
import sys
import termios
import threading

import box
import colorlog
import esptool
import pyudev
import serial

LOG_LEVEL = logging.WARNING
DEFAULT_ESP_JSON = "out/esp32/flasher_args.json"
DEFAULT_SAM_IMAGE = "out/samd21/io_coprocessor.bin"
DEFAULT_SAM_FLASHER = "external/bossac/bossac"
# Files for flashing are validated once the logger is set up
ESP_JSON  = pathlib.Path(os.environ.get("ESP_JSON", DEFAULT_ESP_JSON))
ESP_IMAGE = os.environ.get("ESP_IMAGE_OVERRIDE")
SAM_IMAGE = pathlib.Path(os.environ.get("SAM_IMAGE", DEFAULT_SAM_IMAGE))
# We need an external program to flash the SAMD21 using the MKRZero loader
SAM_FLASHER = pathlib.Path(os.environ.get("SAM_FLASHER", DEFAULT_SAM_FLASHER))
# The SAM flasher doesn't deal well with devices being disconnected so we'll
# use a 10 second timeout which should be more than enough
SAM_FLASHER_TIMEOUT = int(os.environ.get("SAM_FLASHER_TIMEOUT", 10))

# Add a custom level called `SUCCESS` which is as important as an error
DETECT = logging.ERROR + 1
logging.addLevelName(DETECT, "DETECT")
SUCCESS = logging.ERROR + 2
logging.addLevelName(SUCCESS, "SUCCESS")
# Set up the logger to render colourful messages
logger = logging.getLogger("badge.daemon")
_h = colorlog.StreamHandler()
_h.setFormatter(colorlog.ColoredFormatter(
    "[%(name)s] @ %(asctime)s %(log_color)s%(levelname)8s: %(message)s",
    log_colors={
        "DETECT": "blue",
        "SUCCESS": "black,bg_green",
        "WARNING": "yellow",
        "ERROR": "red,bg_white",
        "CRITICAL": "red,bg_white",
        "EXCEPTION": "red",
    },

))
logger.addHandler(_h)
logger.setLevel(LOG_LEVEL)

# Load the ESP flasher args - this doesn't actually validate them but at least
# we can confirm the file is valid JSON
missing_files = False
try:
    ESP_FLASH_ARGS = json.load(ESP_JSON.open())
except FileNotFoundError as exc:
    logger.error("Missing ESP programming configuration: %s", ESP_JSON)
    missing_files = True
# Canonicalise the filenames in the ESP JSON args. Note that we only do this
# for the paths under `flash_files` since that's what we use below
_flash_files = ESP_FLASH_ARGS["flash_files"]
for k, v in _flash_files.items():
    _flash_files[k] = ESP_JSON.parent / v
# Override the ESP app image if we've been told to
if ESP_IMAGE is not None:
    ESP_FLASH_ARGS["app"]["file"] = ESP_IMAGE
    _app_offset = ESP_FLASH_ARGS["app"]["offset"]
    ESP_FLASH_ARGS["flash_files"][_app_offset] = ESP_IMAGE
# Now check for existence of the images we'll actually be using
for v in _flash_files.values():
    _image_p = pathlib.Path(v)
    if not _image_p.is_file():
        logger.error("Missing ESP binary image: %s", _image_p)
        missing_files = True

# Ensure the MKRZero image exists
if not SAM_IMAGE.is_file():
    logger.error("Missing SAM application image: %s", SAM_IMAGE)
    missing_files = True
# And that we have the program to flash it
if not SAM_FLASHER.is_file():
    logger.error("Missing SAM flashing program: %s", SAM_FLASHER)
    missing_files = True

# If we're missing any files then refuse to proceed
if missing_files:
    logger.error("Exiting: Missing required files")
    sys.exit(1)

#####
# Chip flashing handlers
#####
def handle_unknown(**k):
    logger.warning("Ignoring unknown device: %s", k["ID_MODEL"])

def handle_arduino_live(**_):
    logger.warning("Arduino detected but it needs to be put in program mode")
    logger.info("Press the SAM RST button twice quickly")
    logger.info("LED6 should glow blue once in programming mode")

def handle_arduino_prog(**k):
    # Work out the TTY device name and attempt to get the chip's info
    try:
        dev_name = k["DEVNAME"]
    except KeyError:
        logger.error("Unable to get device name for Arduino serial")
        return
    logger.log(DETECT, "Detected programmable Arduino at %r", dev_name)
    _flasher_base_cmd = (SAM_FLASHER, f"--port={dev_name}", )
    logger.info("Getting chip information")
    try:
        output = subprocess.check_output(
            (*_flasher_base_cmd, "--info"), universal_newlines=True,
            timeout=SAM_FLASHER_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error(
            "Failed to get device info for Arduino at %r: %s", dev_name, exc
        )
        return
    for l in output.splitlines():
        try:
            k, v = (f.strip() for f in l.split(":"))
        except ValueError:
            pass
        else:
            if k in {"Device", "Chip ID", }:
                logger.debug("Arduino at %r has %s: %r", dev_name, k, v)
    # Now we'll flash the application image
    logger.info("Flashing application image to Arduino at %r", dev_name)
    try:
        subprocess.check_call(
            (
                *_flasher_base_cmd,
                "--erase", "--write", "--verify", SAM_IMAGE,
                "--boot=1", # reboot from flash next time
                # We don't reset the chip since it would come back as a live
                # device and pollute the output feed - left as a note though
                #"--reset",
            ),
            stdout=subprocess.DEVNULL, timeout=SAM_FLASHER_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Failed to flash Arduino at %r: %s", dev_name, exc)
    else:
        logger.log(SUCCESS, "Finished flashing Arduino at %r", dev_name)

def handle_cp2105(**k):
    # Our boards don't use interface 1 on these chips so we can skip it
    int_num = int(k["ID_USB_INTERFACE_NUM"])
    if int_num not in {0, }:
        logger.debug("Skipping unattached CP2015 UART interface")
        return
    # Work out the TTY device name and attempt to detect an ESP
    try:
        dev_name = k["DEVNAME"]
    except KeyError:
        logger.error("Unable to get device name for CP2105 serial")
        return
    logger.info("Detected CP2015 USB-UART at %r", dev_name)
    logger.info("Attempting to scan for ESP device")
    stashed_exc = None
    # We make our own `serial.Serial` object so we can ensure it gets closed
    try:
        port = serial.serial_for_url(dev_name)
    except serial.SerialException as exc:
        logger.error(
            "Failed to open serial port for ESP at %r: %s",
            dev_name, exc,
        )
        return
    # Ensure the port is closed before we return so the handle is not leaked
    try:
        for i in range(esptool.DEFAULT_CONNECT_ATTEMPTS):
            try:
                esp = esptool.ESPLoader.detect_chip(
                    port=port, connect_attempts=1
                )
            except esptool.FatalError as exc:
                stashed_exc = exc
                logger.warning(
                    "Unable to detect ESP device at %r (%i/%i)",
                    dev_name, i + 1, esptool.DEFAULT_CONNECT_ATTEMPTS,
                )
                if i == 0:
                    logger.info(
                        "Hold down ESP BOOT button then press ESP EN to reset"
                    )
            # Annoyingly we have to catch a lot of exceptions here because
            # `esptool` allows them to bubble up. These are all most likely to
            # be caused by the device being unplugged.
            except (OSError, termios.error, serial.SerialException) as exc:
                logger.error(
                    "Serial connection error for ESP device at %r: %s",
                    dev_name, exc,
                )
                logger.warning("Was the device unplugged?")
                return
            else:
                break
        else:
            logger.error(
                "Failed to detect ESP device at %r: %s", dev_name, stashed_exc
            )
            return
        logger.log(DETECT, "Detected ESP device at %r", dev_name)
        # Now we have an ESP, let's spit out some information before proceeding
        try:
            chip_id = esp.chip_id()
        except esptool.NotSupportedError:
            logger.debug("Chip ID is not supported - falling back to MAC")
            chip_id = esp.read_mac()
        logger.debug("%r: Chip ID is %r", dev_name, chip_id)
        # Run the ESP stub and use the new ESP object from this point on
        try:
            esp = esp.run_stub()
        except (esptool.FatalError, serial.SerialException) as exc:
            logger.error("Unable to run stub on ESP at %r: %s", dev_name, exc)
        logger.info("Download stub running on ESP at %r", dev_name)
        # Increase the baud rate for flashing
        esp.change_baud(921600)
        # Attempt to flash all of the images in one go. We don't bother checking
        # for overlap like `esptool.py` does in its `AddrFilenamePairAction`.
        addr_fileobj_pairs = tuple(
            (int(offset, 0), path.open("rb")) for offset, path
            in ESP_FLASH_ARGS["flash_files"].items()
        )
        args = box.Box(
            addr_filename=addr_fileobj_pairs,
            no_progress=True,   # Be quiet
            erase_all=True,     # We want entirely clean flashes
            verify=True,        # This may be a bit slower but worth the time
            compress=True,      # Compress the image for transfer
            encrypt=False,      # We don't encrypt anything
            no_stub=False,      # We pushed the stub already
            flash_size="keep",
            flash_mode="keep",
            flash_freq="keep",
        )
        logger.info("Flashing application image to ESP at %r", dev_name)
        try:
            esptool.write_flash(esp, args)
        except (esptool.FatalError, serial.SerialException) as exc:
            logger.error(
                "Failed to write flash on ESP at %r: %s", dev_name, exc
            )
        else:
            logger.log(SUCCESS, "Finished flashing ESP at %r", dev_name)
    finally:
        port.close()

DEVICE_TYPES = {
    (0x2341, 0x004f): handle_arduino_prog,
    (0x2341, 0x804f): handle_arduino_live,
    (0x10c4, 0xea70): handle_cp2105,
}

#####
# Device discovery and daemon logic
#####
def watch_udev():
    ctx = pyudev.Context()
    mon = pyudev.Monitor.from_netlink(ctx)
    mon.filter_by("tty")
    # Once we've set up the monitor, we can list everything that already
    # present. We'll just hope we don't doubly handle anything <:)
    yield from (
        d.properties for d in ctx.list_devices(subsystem="tty", ID_BUS="usb")
    )
    # This `iter()` trick lets us call `monitor.poll` repeatedly
    for dev in iter(mon.poll, None):
        if dev.action == "add" and dev.properties["ID_BUS"] == "usb":
            yield dev.properties
        else:
            logger.debug("Ignoring non-USB serial device")

def run():
    for props in watch_udev():
        try:
            vid = int(props["ID_VENDOR_ID"], 16)
            pid = int(props["ID_MODEL_ID"], 16)
            handler = DEVICE_TYPES.get((vid, pid), handle_unknown)
            threading.Thread(target=handler, kwargs=props).start()
        except KeyError:
            logger.warning("Failed to handle USB serial device")
            logger.info("Device properties:\n%s", pprint.pformat(dict(props)))

if __name__ == "__main__":
    # Parse arguments
    parser = argparse.ArgumentParser("BSides Badge Flashing Daemon")
    verbosity_grp = parser.add_mutually_exclusive_group()
    verbosity_grp.add_argument("--verbose", "-v", action="count", default=0)
    verbosity_grp.add_argument("--quiet", "-q", action="count", default=0)
    args = parser.parse_args()
    logger.setLevel(logger.level + (args.quiet - args.verbose) * 10)
    # Redirect stdout to `/dev/null` because `esptool` likes to print junk and
    # doesn't have an option to be quiet
    sys.stdout = open(os.devnull, "w")
    try:
        run()
    except (KeyboardInterrupt, SystemExit):
        logger.warning(
            "Exiting: Waiting for threads to end (^C to force exit)"
        )
