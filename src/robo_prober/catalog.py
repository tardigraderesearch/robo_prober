"""Reference catalog of common controller boards and USB serial interfaces.

This catalog is reference metadata only. It is never treated as permission to
drive pins: every pin record denies probing, reading, writing and PWM by
default, and ``assert_no_automatic_probe`` enforces that invariant.

A USB VID/PID usually identifies a USB-to-UART bridge, NOT the controller
behind it. Discovery reports USB matches as interface hints only; the exact
board must always be selected manually.

To add a board, follow the existing helpers and cite the vendor documentation
in ``SOURCES``.
"""

from copy import deepcopy

SOURCES = {
    'esp32': 'https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html',
    'esp32s2': 'https://docs.espressif.com/projects/esp-idf/en/stable/esp32s2/api-reference/peripherals/gpio.html',
    'esp32s3': 'https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/gpio.html',
    'esp32c3': 'https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-reference/peripherals/gpio.html',
    'esp32c6': 'https://docs.espressif.com/projects/esp-idf/en/latest/esp32c6/api-reference/peripherals/gpio.html',
    'devkitc_v4': 'https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32/esp32-devkitc/user_guide.html',
    's3_devkitc': 'https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.0.html',
    'pico': 'https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html',
    'uno': 'https://docs.arduino.cc/hardware/uno-rev3',
}


def _pin(number, *, label=None, role='gpio', gpio=None, exposed=None,
         adc=None, pwm=None, notes='', reserved=False, source=None, functions=()):
    """All permission booleans deliberately default to DENY."""
    return {
        'label': label if label is not None else (f'GPIO{number}' if role == 'gpio' else str(number)),
        'type': role,
        'gpio': gpio if gpio is not None else (number if role == 'gpio' else None),
        'exposed': exposed,
        'adc': adc,
        'pwm_capable': pwm,
        'alternate_functions': list(functions),
        'reserved': bool(reserved),
        'notes': notes,
        'safe_probe': False,
        'safe_read': False,
        'safe_digital_write': False,
        'safe_pwm': False,
    }


def _profile(name, platform, pins, source_keys, *, pin_index='gpio',
             logic_voltage=3.3, transport='serial_firmware', verified_exposure=False,
             notes=''):
    return {
        'name': name, 'platform': platform, 'pin_index': pin_index,
        'logic_voltage': logic_voltage, 'transport': transport,
        'board_pinout_verified': bool(verified_exposure),
        'auto_probe': False, 'notes': notes,
        'sources': [SOURCES[k] for k in source_keys], 'pins': pins,
    }


def _esp_profile(name, model, gpios, *, strap=(), bus=(), usb=(), uart=(),
                 jtag=(), adc1=None, adc2=None, input_only=(),
                 source_key='', note=''):
    pins = {}
    strap, bus, usb, uart, jtag, input_only = map(set, (strap, bus, usb, uart, jtag, input_only))
    adc1 = adc1 or {}
    adc2 = adc2 or {}
    for n in gpios:
        flags = []
        if n in strap: flags.append('boot strapping')
        if n in bus: flags.append('flash / PSRAM interface')
        if n in usb: flags.append('USB Serial/JTAG')
        if n in uart: flags.append('default UART')
        if n in jtag: flags.append('JTAG')
        if n in input_only: flags.append('INPUT ONLY')
        analog = ('ADC1_CH'+str(adc1[n])) if n in adc1 else (
            'ADC2_CH'+str(adc2[n]) if n in adc2 else None)
        pins[n] = _pin(n, adc=analog, pwm=(n not in input_only and n not in bus),
                       reserved=(n in (strap | bus | usb | uart | jtag | input_only)),
                       notes=', '.join(flags), functions=flags)
    return _profile(name, model, pins, [source_key], notes=note)


# ---- Complete chip GPIO inventories (NOT claims that every GPIO is exposed) ----
_ESP32 = list(range(0, 20)) + [21,22,23,25,26,27,32,33,34,35,36,37,38,39]
_ESP_S2 = list(range(0, 22)) + list(range(26, 47))
_ESP_S3 = list(range(0, 22)) + list(range(26, 49))
_ESP_C3 = list(range(22))
_ESP_C6 = list(range(31))

BOARD_PROFILES = {}
BOARD_PROFILES['esp32_chip_reference'] = _esp_profile(
    'ESP32 (classic) SoC GPIO reference', 'ESP32', _ESP32,
    strap=(0,2,5,12,15), bus=range(6,12), uart=(1,3), jtag=(12,13,14,15),
    adc1={36:0,37:1,38:2,39:3,32:4,33:5,34:6,35:7},
    adc2={4:0,0:1,2:2,15:3,13:4,12:5,14:6,27:7,25:8,26:9},
    input_only=(34,35,36,37,38,39), source_key='esp32',
    note='SoC inventory; never assume these are exposed on a particular ESP32 board.')
BOARD_PROFILES['esp32_s2_chip_reference'] = _esp_profile(
    'ESP32-S2 SoC GPIO reference', 'ESP32-S2', _ESP_S2,
    strap=(0,45,46), bus=range(26,33), usb=(19,20), uart=(43,44), jtag=(39,40,41,42),
    input_only=(46,), adc1={n:n-1 for n in range(1,11)},
    adc2={n:n-11 for n in range(11,21)}, source_key='esp32s2')
BOARD_PROFILES['esp32_s3_chip_reference'] = _esp_profile(
    'ESP32-S3 SoC GPIO reference', 'ESP32-S3', _ESP_S3,
    strap=(0,3,45,46), bus=range(26,38), usb=(19,20), uart=(43,44),
    input_only=(46,), adc1={n:n-1 for n in range(1,11)},
    adc2={n:n-11 for n in range(11,21)}, source_key='esp32s3',
    note='GPIO33-37 are module-dependent; octal flash/PSRAM occupies them.')
BOARD_PROFILES['esp32_c3_chip_reference'] = _esp_profile(
    'ESP32-C3 SoC GPIO reference', 'ESP32-C3', _ESP_C3,
    strap=(2,8,9), bus=range(12,18), usb=(18,19), uart=(20,21),
    adc1={n:n for n in range(5)}, adc2={5:0}, source_key='esp32c3')
BOARD_PROFILES['esp32_c6_chip_reference'] = _esp_profile(
    'ESP32-C6 SoC GPIO reference', 'ESP32-C6', _ESP_C6,
    strap=(4,5,8,9,15), bus=range(24,31), usb=(12,13), uart=(16,17),
    jtag=(4,5,6,7), adc1={n:n for n in range(7)}, source_key='esp32c6',
    note='Packaging/SiP flash affects exposed GPIO10/11 and GPIO14.')


def _board_from_chip(chip_id, name, exposed, source_keys, *, hidden=(), reasons=None, notes=''):
    parent = BOARD_PROFILES[chip_id]
    pins = deepcopy(parent['pins'])
    exposed, hidden = set(exposed), set(hidden)
    reasons = reasons or {}
    for n, pin in pins.items():
        pin['exposed'] = (n in exposed) and n not in hidden
        if n in hidden:
            pin['reserved'] = True
            pin['notes'] = ((pin['notes'] + '; ') if pin['notes'] else '') + reasons.get(n, 'not usable on this board')
        elif n not in exposed:
            pin['notes'] = ((pin['notes'] + '; ') if pin['notes'] else '') + 'not exposed on board header'
    return _profile(name, parent['platform'], pins, source_keys,
                    verified_exposure=True, notes=notes)

# Exact Espressif DevKit headers -- exclude all other chip pins as unavailable.
_devkit32_exposed = [36,39,34,35,32,33,25,26,27,14,12,13,23,22,1,3,21,19,18,5,17,16,4,0,2,15,9,10,11,8,7,6]
BOARD_PROFILES['esp32_devkitc_v4_wroom32'] = _board_from_chip(
    'esp32_chip_reference','ESP32-DevKitC V4 (WROOM-32)',_devkit32_exposed,
    ['esp32','devkitc_v4'],hidden=(6,7,8,9,10,11),
    reasons={n:'header connected to internal SPI flash; DO NOT USE' for n in (6,7,8,9,10,11)},
    notes='GPIO16/17 only on WROOM/SOLO, not WROVER; verify module variant.')
for n in (16,17):
    BOARD_PROFILES['esp32_devkitc_v4_wroom32']['pins'][n]['notes'] += '; WROVER modules reserve this pin'

_s3_header=[4,5,6,7,15,16,17,18,8,3,46,9,10,11,12,13,14,43,44,1,2,42,41,40,39,38,37,36,35,0,45,48,47,21,20,19]
BOARD_PROFILES['esp32_s3_devkitc_1'] = _board_from_chip(
    'esp32_s3_chip_reference', 'ESP32-S3-DevKitC-1 (module variant unconfirmed)',
    _s3_header, ['esp32s3','s3_devkitc'],hidden=(35,36,37,48),
    reasons={35:'octal PSRAM variant may consume GPIO35',36:'octal PSRAM variant may consume GPIO36',
             37:'octal PSRAM variant may consume GPIO37',48:'on-board RGB LED'},
    notes='Conservative all-variants default. Module revision and flash/PSRAM must be confirmed.')

# ---- Raspberry Pi Pico (RP2040) board pinout ----
_PICO_HEADER={
    1:0,2:1,3:'GND',4:2,5:3,6:4,7:5,8:'GND',9:6,10:7,
    11:8,12:9,13:'GND',14:10,15:11,16:12,17:13,18:'GND',19:14,20:15,
    21:16,22:17,23:'GND',24:18,25:19,26:20,27:21,28:'GND',29:22,
    30:'RUN',31:26,32:27,33:'AGND',34:28,35:'ADC_VREF',36:'3V3_OUT',
    37:'3V3_EN',38:'GND',39:'VSYS',40:'VBUS'
}
def _pico_header():
    pins={}
    for pos,signal in _PICO_HEADER.items():
        if isinstance(signal,int):
            pins[pos]=_pin(pos,label=f'GP{signal}',gpio=signal,
                adc=(f'ADC{signal-26}' if signal in (26,27,28) else None),
                pwm=True,exposed=True)
        else:
            pins[pos]=_pin(pos,label=signal,role='control' if signal=='RUN' else 'power',
                           exposed=True,reserved=True,notes='Do not GPIO probe')
    return pins
for profile_id,name in [('raspberry_pi_pico','Raspberry Pi Pico'),
                        ('raspberry_pi_pico_w','Raspberry Pi Pico W')]:
    p=_profile(name,'RP2040 MCU',_pico_header(),['pico'],
               pin_index='physical_header',verified_exposure=True,
               notes='40 physical header positions. GPIO23-25 and 29 internal/not broken out; Wi-Fi variants reserve extra circuitry.')
    p['internal_pins']={g:_pin(g,exposed=False,reserved=True,
                             notes='Internal/onboard function, not on 40-pin header') for g in (23,24,25,29)}
    BOARD_PROFILES[profile_id]=p

# ---- Arduino classic boards: all user digital and analog header signals ----
_UNO_PWM={3,5,6,9,10,11}
_uno={}
for i in range(14):
    _uno[f'D{i}']=_pin(i,label=f'D{i}',role='digital',gpio=i,pwm=i in _UNO_PWM,
              exposed=True,reserved=i in (0,1),
              notes='Serial RX/TX used for USB' if i in (0,1) else '')
for i in range(6):
    _uno[f'A{i}']=_pin(i,label=f'A{i}',role='analog',exposed=True,adc=f'ADC{i}',
                    pwm=False,notes='SDA' if i==4 else 'SCL' if i==5 else '')
BOARD_PROFILES['arduino_uno_r3']=_profile('Arduino Uno Rev3','ATmega328P',_uno,
   ['uno'],pin_index='arduino_label',logic_voltage=5.0,verified_exposure=True,
   notes='D0-D13 and A0-A5 enumerated. I2C on A4/A5; extra power/reference pins excluded from I/O-only namespace.')

# USB device *interfaces* only. The SAME VID/PID can appear on many controllers.
USB_INTERFACES = {
    (0x303A, 0x1001): {
        'name': 'Espressif native USB Serial/JTAG interface',
        'match_type': 'usb_interface_only',
        'candidate_profiles': ['esp32_s3_chip_reference', 'esp32_s3_devkitc_1',
                               'esp32_c3_chip_reference', 'esp32_c6_chip_reference'],
        'pins': None,
        'notes': 'Chip/board MUST be confirmed. USB interface does not prove ESP32-S3.'},
    (0x10C4, 0xEA60): {
        'name': 'Silicon Labs CP210x USB/UART bridge',
        'match_type': 'usb_bridge_only',
        'candidate_profiles': ['esp32_devkitc_v4_wroom32', 'esp32_s3_devkitc_1',
                               'esp32_chip_reference', 'esp32_s2_chip_reference',
                               'esp32_c3_chip_reference'],
        'pins': None, 'notes': 'Generic adapter; any downstream controller possible.'},
    (0x1A86, 0x55D3): {
        'name': 'WCH CH343 USB/UART bridge',
        'match_type': 'usb_bridge_only',
        'candidate_profiles': ['esp32_chip_reference', 'esp32_s3_chip_reference'],
        'pins': None, 'notes': 'Generic bridge used on many ESP32 boards; confirm the chip with esptool.'},
    (0x1A86, 0x7523): {
        'name': 'WCH CH340 USB/UART bridge',
        'match_type': 'usb_bridge_only',
        'candidate_profiles': ['esp32_chip_reference', 'esp32_s2_chip_reference',
                               'esp32_s3_chip_reference', 'esp32_c3_chip_reference'],
        'pins': None, 'notes': 'Generic bridge; do not infer board or pinout.'},
    (0x0403, 0x6001): {
        'name': 'FTDI FT232 USB/UART bridge',
        'match_type': 'usb_bridge_only',
        'candidate_profiles': [],
        'pins': None, 'notes': 'Generic bridge; unknown downstream controller.'},
    (0x2341, 0x0043): {
        'name': 'Arduino Uno Rev3 USB interface (common official VID/PID)',
        'match_type': 'board_candidate_not_proof',
        'candidate_profiles': ['arduino_uno_r3'],
        'pins': None, 'notes': 'Clones may use other USB IDs; manual board confirmation required.'},
    (0x2E8A, 0x0005): {
        'name': 'Raspberry Pi Pico MicroPython USB CDC interface',
        'match_type': 'board_candidate_not_proof',
        'candidate_profiles': ['raspberry_pi_pico', 'raspberry_pi_pico_w'],
        'pins': None, 'notes': 'Depends on the running firmware; manual board confirmation required.'},
}


def get_profile(board_id):
    """Return a complete independent reference pinout for an exact board ID."""
    if board_id not in BOARD_PROFILES:
        raise KeyError('Unknown board profile: '+str(board_id))
    return deepcopy(BOARD_PROFILES[board_id])


def match_usb(vid, pid):
    """Get a USB INTERFACE hint, never an inferred pinout."""
    return deepcopy(USB_INTERFACES.get((vid,pid)))


def list_board_ids():
    return sorted(BOARD_PROFILES)


def candidate_gpio(board_id):
    """List all physically exposed GPIO candidates; this does NOT authorize probing."""
    profile=get_profile(board_id)
    return {n:p for n,p in profile['pins'].items()
            if p['exposed'] is True and p['type']=='gpio' and not p['reserved']}


# Raspberry Pi / Jetson header profiles live in catalog_sbc.py when present.
try:
    from . import catalog_sbc as _sbc
except ImportError:
    _sbc = None
_SBC_PROFILES = (getattr(_sbc, 'BOARD_PROFILES_SBC', None) or getattr(_sbc, 'SBC_PROFILES', None)
                 or getattr(_sbc, 'BOARD_PROFILES', None) or {})
for _board_id, _sbc_profile in _SBC_PROFILES.items():
    BOARD_PROFILES.setdefault(_board_id, _sbc_profile)


# Discovery flags for the firmware pin tables (`python -m robo_prober.firmware_pins`
# turns these into firmware/esp32_robo_prober/src/pin_profiles.h). They describe what
# a pin IS, never a permission: the firmware still only reads pins until ARM,
# and drives one pin at a time on the operator's request.
PIN_USABLE = 1      # on the chip and not wired to flash/PSRAM, USB or the host UART
PIN_LAST = 2        # boot strapping or JTAG: surveyed last, driven only after a confirmation
PIN_INPUT_ONLY = 4  # can be read, never driven
PIN_ADC = 8         # has an ADC channel
_NEVER_TOUCH = ('flash / PSRAM interface', 'USB Serial/JTAG', 'default UART')

FIRMWARE_CHIPS = {  # firmware build target -> chip reference profile (BOARD reports the profile id)
    'esp32': 'esp32_chip_reference',
    'esp32s2': 'esp32_s2_chip_reference',
    'esp32s3': 'esp32_s3_chip_reference',
    'esp32c3': 'esp32_c3_chip_reference',
    'esp32c6': 'esp32_c6_chip_reference',
}


def discovery_pin_flags(board_id):
    """{gpio: PIN_* bits} for every GPIO of a chip reference profile (0 = never touch)."""
    flags={}
    for n,p in BOARD_PROFILES[board_id]['pins'].items():
        functions=set(p['alternate_functions'])
        if functions & set(_NEVER_TOUCH):
            flags[n]=0
            continue
        bits=PIN_USABLE
        if functions & {'boot strapping', 'JTAG'}: bits|=PIN_LAST
        if 'INPUT ONLY' in functions: bits|=PIN_INPUT_ONLY
        if p['adc']: bits|=PIN_ADC
        flags[n]=bits
    return flags


def assert_no_automatic_probe():
    """Validate fail-closed invariant across the *entire* library."""
    for board_id, profile in BOARD_PROFILES.items():
        for n,p in profile['pins'].items():
            if any(p[key] for key in ('safe_probe','safe_read','safe_digital_write','safe_pwm')):
                raise AssertionError(f'{board_id} pin {n} is unexpectedly auto-approved')
    return True


if __name__ == '__main__':
    assert_no_automatic_probe()
    for board_id in list_board_ids():
        p=BOARD_PROFILES[board_id]
        print(f'{board_id:30s} {len(p["pins"]):>3} pin records  ({p["pin_index"]})')
