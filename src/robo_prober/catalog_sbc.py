"""Reference catalog for Single Board Computers (Raspberry Pi, NVIDIA Jetson).

This module restores and maintains complete 40-pin expansion header profiles for
Linux single-board computers (SBCs). Profiles provide hardware pinout metadata,
Linux gpiochip and line names, default multiplexed functions, bus assignments,
and fail-closed security invariants.
"""

from copy import deepcopy
import robo_prober.catalog as _catalog
from robo_prober.catalog import _pin, _profile, candidate_gpio as _candidate_gpio, get_profile as _get_profile

SOURCES = {
    'rpi': 'https://www.raspberrypi.com/documentation/computers/raspberry-pi.html',
    'rpi4': 'https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-4-model-b',
    'rpi5': 'https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-5',
    'rpi_zero_2w': 'https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-zero-2-w',
    'jetson': 'https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/hardware_layout.html',
    'jetson_carrier': 'https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf',
    'jetson_orin_nx': 'https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/hardware_layout.html',
    'jetson_agx_orin': 'https://developer.nvidia.com/embedded/learn/jetson-agx-orin-devkit-user-guide/index.html',
}

# Ensure catalog._profile can look up documentation URLs cited by SBC profiles.
_catalog.SOURCES.update(SOURCES)

# ---- Raspberry Pi 40-pin header inventory ----
# Physical pin position -> (kind, BCM GPIO number)
_PI_HEADER = {
    1: ('3v3', None),   2: ('5v', None),    3: ('gpio', 2),   4: ('5v', None),
    5: ('gpio', 3),     6: ('gnd', None),   7: ('gpio', 4),   8: ('gpio', 14),
    9: ('gnd', None),  10: ('gpio', 15),  11: ('gpio', 17),  12: ('gpio', 18),
    13: ('gpio', 27),  14: ('gnd', None),  15: ('gpio', 22),  16: ('gpio', 23),
    17: ('3v3', None),  18: ('gpio', 24),  19: ('gpio', 10),  20: ('gnd', None),
    21: ('gpio', 9),   22: ('gpio', 25),  23: ('gpio', 11),  24: ('gpio', 8),
    25: ('gnd', None),  26: ('gpio', 7),   27: ('gpio', 0),   28: ('gpio', 1),
    29: ('gpio', 5),   30: ('gnd', None),  31: ('gpio', 6),   32: ('gpio', 12),
    33: ('gpio', 13),  34: ('gnd', None),  35: ('gpio', 19),  36: ('gpio', 16),
    37: ('gpio', 26),  38: ('gpio', 20),  39: ('gnd', None),  40: ('gpio', 21),
}

# Physical positions with dedicated hardware functions and characteristics
_PI_PWM_PINS = {12, 32, 33, 35}  # GPIO 18 (PWM0), GPIO 12 (PWM0), GPIO 13 (PWM1), GPIO 19 (PWM1)
_PI_ID_EEPROM_PINS = {27, 28}    # GPIO 0 (ID_SD), GPIO 1 (ID_SC) - never touch


def _pi_header(gpiochip_label: str = 'pinctrl-bcm2835') -> dict[int, dict]:
    """Return full 40-pin header dictionary for a Raspberry Pi board."""
    pins = {}
    for physical, (kind, bcm_gpio) in _PI_HEADER.items():
        if kind != 'gpio':
            p = _pin(
                physical,
                label=kind.upper(),
                role=kind,
                gpio=None,
                exposed=True,
                reserved=True,
                notes=f'{kind.upper()} power/ground: never drive',
            )
            p['gpiochip'] = None
            p['usable'] = False
            pins[physical] = p
            continue

        # ID EEPROM pins (GPIO 0 / 1) - strictly reserved for HAT identification
        if physical in _PI_ID_EEPROM_PINS:
            p = _pin(
                physical,
                label=f'GPIO{bcm_gpio}',
                role='gpio',
                gpio=bcm_gpio,
                exposed=True,
                reserved=True,
                notes=f'HAT ID EEPROM (GPIO{bcm_gpio}); dedicated board identification, never touch',
                functions=['ID_SD' if bcm_gpio == 0 else 'ID_SC', 'HAT ID EEPROM'],
            )
            p['gpiochip'] = gpiochip_label
            p['line_name'] = f'GPIO{bcm_gpio}'
            p['default_function'] = 'ID_EEPROM'
            p['i2c_bus'] = 0
            p['usable'] = False
            pins[physical] = p
            continue

        # Default I2C1 (GPIO 2 / 3) with on-board 1.8k pull-up resistors
        if physical in (3, 5):
            role_name = 'SDA1' if bcm_gpio == 2 else 'SCL1'
            p = _pin(
                physical,
                label=f'GPIO{bcm_gpio}',
                role='gpio',
                gpio=bcm_gpio,
                exposed=True,
                reserved=False,
                notes=f'Default I2C1 {role_name}; on-board 1.8k pull-up (always surveys as pulled_up)',
                functions=[f'I2C1_{role_name}'],
            )
            p['gpiochip'] = gpiochip_label
            p['line_name'] = f'GPIO{bcm_gpio}'
            p['default_function'] = f'I2C1_{role_name}'
            p['i2c_bus'] = 1
            p['usable'] = True
            pins[physical] = p
            continue

        # Default UART (GPIO 14 / 15)
        if physical in (8, 10):
            uart_fn = 'UART0_TXD' if bcm_gpio == 14 else 'UART0_RXD'
            p = _pin(
                physical,
                label=f'GPIO{bcm_gpio}',
                role='gpio',
                gpio=bcm_gpio,
                exposed=True,
                reserved=False,
                notes=f'Primary UART {uart_fn[-3:]} ({uart_fn})',
                functions=[uart_fn],
            )
            p['gpiochip'] = gpiochip_label
            p['line_name'] = f'GPIO{bcm_gpio}'
            p['default_function'] = uart_fn
            p['usable'] = True
            pins[physical] = p
            continue

        # Hardware PWM capable pins (GPIO 12, 13, 18, 19)
        if physical in _PI_PWM_PINS:
            pwm_chan = 'PWM0' if bcm_gpio in (12, 18) else 'PWM1'
            extra_fn = ['PCM_CLK'] if bcm_gpio == 18 else (['PCM_FS'] if bcm_gpio == 19 else [])
            p = _pin(
                physical,
                label=f'GPIO{bcm_gpio}',
                role='gpio',
                gpio=bcm_gpio,
                exposed=True,
                pwm=True,
                reserved=False,
                notes=f'Hardware {pwm_chan} capable' + (f'; {extra_fn[0]}' if extra_fn else ''),
                functions=[pwm_chan] + extra_fn,
            )
            p['gpiochip'] = gpiochip_label
            p['line_name'] = f'GPIO{bcm_gpio}'
            p['default_function'] = 'GPIO'
            p['usable'] = True
            pins[physical] = p
            continue

        # General Purpose I/O pins
        extra_fns = []
        notes = 'General purpose GPIO'
        if physical in (19, 21, 23, 24, 26):
            spi_map = {19: 'SPI0_MOSI', 21: 'SPI0_MISO', 23: 'SPI0_SCLK', 24: 'SPI0_CE0', 26: 'SPI0_CE1'}
            extra_fns.append(spi_map[physical])
            notes = f'General purpose GPIO; alternate {spi_map[physical]}'
        elif physical == 7:
            extra_fns.append('GPCLK0')
            notes = 'General purpose GPIO; alternate GPCLK0'
        elif physical in (38, 40):
            pcm_fn = 'PCM_DIN' if physical == 38 else 'PCM_DOUT'
            extra_fns.append(pcm_fn)
            notes = f'General purpose GPIO; alternate {pcm_fn}'

        p = _pin(
            physical,
            label=f'GPIO{bcm_gpio}',
            role='gpio',
            gpio=bcm_gpio,
            exposed=True,
            pwm=False,
            reserved=False,
            notes=notes,
            functions=extra_fns,
        )
        p['gpiochip'] = gpiochip_label
        p['line_name'] = f'GPIO{bcm_gpio}'
        p['default_function'] = 'GPIO'
        p['usable'] = True
        pins[physical] = p

    return pins


# ---- NVIDIA Jetson Orin Nano / NX dev kit (P3768 carrier board, J12 header) ----
# Data restored and validated against NVIDIA Jetson Orin Nano Carrier Board Specification (Table 3-3).
# Only pins documented as GPIO by default are marked usable. Multiplexed SFIO lines (UART, SPI, I2S, I2C)
# require manual pinmux reconfiguration or jetson-io and remain reserved by default.
_JETSON_ORIN_J12 = {
    1: {'label': '3V3', 'role': '3v3', 'gpio': None, 'chip': None, 'line': None, 'fn': '3.3V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '3.3V power rail (max ~100mA); never drive'},
    2: {'label': '5V', 'role': '5v', 'gpio': None, 'chip': None, 'line': None, 'fn': '5.0V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '5.0V power rail; never drive'},
    3: {'label': 'I2C1_SDA', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'GPCC.00', 'fn': 'I2C1_SDA',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 7, 'usable': False,
        'notes': 'I2C bus 7 SDA (on-board 3.3V pull-up); SFIO by default'},
    4: {'label': '5V', 'role': '5v', 'gpio': None, 'chip': None, 'line': None, 'fn': '5.0V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '5.0V power rail; never drive'},
    5: {'label': 'I2C1_SCL', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'GPCC.01', 'fn': 'I2C1_SCL',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 7, 'usable': False,
        'notes': 'I2C bus 7 SCL (on-board 3.3V pull-up); SFIO by default'},
    6: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'Ground; never drive'},
    7: {'label': 'GPIO09', 'role': 'gpio', 'gpio': 144, 'chip': 'tegra234-gpio', 'line': 'PAC.06', 'fn': 'GPIO',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
        'notes': 'GPIO09 / PAC.06; GPIO by default (Alt: AUD_MCLK)'},
    8: {'label': 'UART1_TX', 'role': 'uart', 'gpio': None, 'chip': None, 'line': 'GP69_UART1_TX', 'fn': 'UART1_TX',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'UART1 TX; dedicated serial SFIO by default'},
    9: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'Ground; never drive'},
    10: {'label': 'UART1_RX', 'role': 'uart', 'gpio': None, 'chip': None, 'line': 'GP70_UART1_RX', 'fn': 'UART1_RX',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 RX; dedicated serial SFIO by default'},
    11: {'label': 'UART1_RTS', 'role': 'uart', 'gpio': 112, 'chip': 'tegra234-gpio', 'line': 'PR.04', 'fn': 'UART1_RTS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 RTS / PR.04; SFIO by default, requires jetson-io to reassign to GPIO'},
    12: {'label': 'I2S0_SCLK', 'role': 'i2s', 'gpio': 50, 'chip': 'tegra234-gpio', 'line': 'PH.07', 'fn': 'I2S0_SCLK',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S0 Bit Clock / PH.07; SFIO by default'},
    13: {'label': 'SPI1_SCK', 'role': 'spi', 'gpio': 122, 'chip': 'tegra234-gpio', 'line': 'PY.00', 'fn': 'SPI1_SCK',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Clock / PY.00; SFIO by default'},
    14: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    15: {'label': 'GPIO12', 'role': 'gpio', 'gpio': 85, 'chip': 'tegra234-gpio', 'line': 'PN.01', 'fn': 'GPIO',
         'pwm': True, 'pwm_chip': '3280000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': True,
         'notes': 'GPIO12 / PN.01; GPIO by default, hardware PWM capable (3280000.pwm)'},
    16: {'label': 'SPI1_CS1', 'role': 'spi', 'gpio': 126, 'chip': 'tegra234-gpio', 'line': 'PY.04', 'fn': 'SPI1_CS1',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Chip Select 1 / PY.04; SFIO by default'},
    17: {'label': '3V3', 'role': '3v3', 'gpio': None, 'chip': None, 'line': None, 'fn': '3.3V Power',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': '3.3V power rail; never drive'},
    18: {'label': 'SPI1_CS0', 'role': 'spi', 'gpio': 125, 'chip': 'tegra234-gpio', 'line': 'PY.03', 'fn': 'SPI1_CS0',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Chip Select 0 / PY.03; SFIO by default'},
    19: {'label': 'SPI0_MOSI', 'role': 'spi', 'gpio': 135, 'chip': 'tegra234-gpio', 'line': 'PZ.05', 'fn': 'SPI0_MOSI',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI0 MOSI / PZ.05; SFIO by default'},
    20: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    21: {'label': 'SPI0_MISO', 'role': 'spi', 'gpio': 134, 'chip': 'tegra234-gpio', 'line': 'PZ.04', 'fn': 'SPI0_MISO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI0 MISO / PZ.04; SFIO by default'},
    22: {'label': 'SPI1_MISO', 'role': 'spi', 'gpio': 123, 'chip': 'tegra234-gpio', 'line': 'PY.01', 'fn': 'SPI1_MISO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 MISO / PY.01; SFIO by default'},
    23: {'label': 'SPI0_SCK', 'role': 'spi', 'gpio': 133, 'chip': 'tegra234-gpio', 'line': 'PZ.03', 'fn': 'SPI0_SCK',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI0 SCK / PZ.03; SFIO by default'},
    24: {'label': 'SPI0_CS0', 'role': 'spi', 'gpio': 136, 'chip': 'tegra234-gpio', 'line': 'PZ.06', 'fn': 'SPI0_CS0',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI0 Chip Select 0 / PZ.06; SFIO by default'},
    25: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    26: {'label': 'SPI0_CS1', 'role': 'spi', 'gpio': 137, 'chip': 'tegra234-gpio', 'line': 'PZ.07', 'fn': 'SPI0_CS1',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI0 Chip Select 1 / PZ.07; SFIO by default'},
    27: {'label': 'I2C0_SDA', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C0_SDA', 'fn': 'I2C0_SDA',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 1, 'usable': False,
         'notes': 'ID EEPROM I2C0 SDA (I2C bus 1); reserved for carrier/HAT identification, never touch'},
    28: {'label': 'I2C0_SCL', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C0_SCL', 'fn': 'I2C0_SCL',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 1, 'usable': False,
         'notes': 'ID EEPROM I2C0 SCL (I2C bus 1); reserved for carrier/HAT identification, never touch'},
    29: {'label': 'GPIO01', 'role': 'gpio', 'gpio': 105, 'chip': 'tegra234-gpio', 'line': 'PQ.05', 'fn': 'GPIO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
         'notes': 'GPIO01 / PQ.05; GPIO by default'},
    30: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    31: {'label': 'GPIO11', 'role': 'gpio', 'gpio': 106, 'chip': 'tegra234-gpio', 'line': 'PQ.06', 'fn': 'GPIO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
         'notes': 'GPIO11 / PQ.06; GPIO by default'},
    32: {'label': 'GPIO07', 'role': 'gpio', 'gpio': 41, 'chip': 'tegra234-gpio', 'line': 'PG.06', 'fn': 'GPIO',
         'pwm': True, 'pwm_chip': '32e0000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': True,
         'notes': 'GPIO07 / PG.06; GPIO by default, hardware PWM capable (32e0000.pwm)'},
    33: {'label': 'GPIO13', 'role': 'gpio', 'gpio': 43, 'chip': 'tegra234-gpio', 'line': 'PH.00', 'fn': 'GPIO',
         'pwm': True, 'pwm_chip': '32c0000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': True,
         'notes': 'GPIO13 / PH.00; GPIO by default, hardware PWM capable (32c0000.pwm)'},
    34: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    35: {'label': 'I2S0_FS', 'role': 'i2s', 'gpio': 53, 'chip': 'tegra234-gpio', 'line': 'PI.02', 'fn': 'I2S0_FS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S0 Frame Sync / PI.02; SFIO by default'},
    36: {'label': 'UART1_CTS', 'role': 'uart', 'gpio': 113, 'chip': 'tegra234-gpio', 'line': 'PR.05', 'fn': 'UART1_CTS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 CTS / PR.05; SFIO by default'},
    37: {'label': 'SPI1_MOSI', 'role': 'spi', 'gpio': 124, 'chip': 'tegra234-gpio', 'line': 'PY.02', 'fn': 'SPI1_MOSI',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 MOSI / PY.02; SFIO by default'},
    38: {'label': 'I2S0_SDIN', 'role': 'i2s', 'gpio': 52, 'chip': 'tegra234-gpio', 'line': 'PI.01', 'fn': 'I2S0_SDIN',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S0 Data In / PI.01; SFIO by default'},
    39: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    40: {'label': 'I2S0_SDOUT', 'role': 'i2s', 'gpio': 51, 'chip': 'tegra234-gpio', 'line': 'PI.00', 'fn': 'I2S0_SDOUT',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S0 Data Out / PI.00; SFIO by default'},
}


def _jetson_orin_header() -> dict[int, dict]:
    """Return full 40-pin J12 header dictionary for Jetson Orin Nano / NX dev kit."""
    pins = {}
    for physical, info in _JETSON_ORIN_J12.items():
        role = info['role']
        is_gpio = (role == 'gpio')
        p = _pin(
            physical,
            label=info['label'],
            role=role,
            gpio=info['gpio'],
            exposed=True,
            pwm=info['pwm'],
            reserved=not info['usable'],
            notes=info['notes'],
            functions=[info['fn']] if info['fn'] else (),
        )
        p['gpiochip'] = info['chip']
        p['line_name'] = info['line']
        p['default_function'] = info['fn']
        p['pwm_chip'] = info['pwm_chip']
        p['pwm_id'] = info['pwm_id']
        p['i2c_bus'] = info['i2c']
        p['usable'] = info['usable']
        pins[physical] = p
    return pins


# ---- NVIDIA Jetson AGX Orin Developer Kit (P3737 carrier board) ----
_JETSON_AGX_ORIN_J30 = {
    1: {'label': '3V3', 'role': '3v3', 'gpio': None, 'chip': None, 'line': None, 'fn': '3.3V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '3.3V power rail; never drive'},
    2: {'label': '5V', 'role': '5v', 'gpio': None, 'chip': None, 'line': None, 'fn': '5.0V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '5.0V power rail; never drive'},
    3: {'label': 'I2C1_SDA', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C1_SDA', 'fn': 'I2C1_SDA',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 1, 'usable': False,
        'notes': 'I2C bus 1 SDA; SFIO by default'},
    4: {'label': '5V', 'role': '5v', 'gpio': None, 'chip': None, 'line': None, 'fn': '5.0V Power',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': '5.0V power rail; never drive'},
    5: {'label': 'I2C1_SCL', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C1_SCL', 'fn': 'I2C1_SCL',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 1, 'usable': False,
        'notes': 'I2C bus 1 SCL; SFIO by default'},
    6: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'Ground; never drive'},
    7: {'label': 'MCLK05', 'role': 'signal', 'gpio': 106, 'chip': 'tegra234-gpio', 'line': 'PQ.06', 'fn': 'MCLK05',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'Audio Master Clock / PQ.06; SFIO by default'},
    8: {'label': 'UART1_TX', 'role': 'uart', 'gpio': None, 'chip': None, 'line': 'UART1_TX', 'fn': 'UART1_TX',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'UART1 TX; SFIO by default'},
    9: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
        'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
        'notes': 'Ground; never drive'},
    10: {'label': 'UART1_RX', 'role': 'uart', 'gpio': None, 'chip': None, 'line': 'UART1_RX', 'fn': 'UART1_RX',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 RX; SFIO by default'},
    11: {'label': 'UART1_RTS', 'role': 'uart', 'gpio': 112, 'chip': 'tegra234-gpio', 'line': 'PR.04', 'fn': 'UART1_RTS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 RTS / PR.04; SFIO by default'},
    12: {'label': 'I2S2_CLK', 'role': 'i2s', 'gpio': 50, 'chip': 'tegra234-gpio', 'line': 'PH.07', 'fn': 'I2S2_CLK',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S2 Clock / PH.07; SFIO by default'},
    13: {'label': 'PWM01', 'role': 'signal', 'gpio': 108, 'chip': 'tegra234-gpio', 'line': 'PR.00', 'fn': 'PWM01',
         'pwm': True, 'pwm_chip': '32f0000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': False,
         'notes': 'PWM01 / PR.00; SFIO by default, hardware PWM capable (32f0000.pwm)'},
    14: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    15: {'label': 'GPIO27', 'role': 'gpio', 'gpio': 85, 'chip': 'tegra234-gpio', 'line': 'PN.01', 'fn': 'GPIO',
         'pwm': True, 'pwm_chip': '3280000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': True,
         'notes': 'GPIO27 / PN.01; GPIO by default, hardware PWM capable (3280000.pwm)'},
    16: {'label': 'GPIO08', 'role': 'gpio', 'gpio': 9, 'chip': 'tegra234-gpio-aon', 'line': 'PBB.01', 'fn': 'GPIO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
         'notes': 'GPIO08 / PBB.01; Always-On (AON) GPIO by default'},
    17: {'label': '3V3', 'role': '3v3', 'gpio': None, 'chip': None, 'line': None, 'fn': '3.3V Power',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': '3.3V power rail; never drive'},
    18: {'label': 'GPIO35', 'role': 'gpio', 'gpio': 43, 'chip': 'tegra234-gpio', 'line': 'PH.00', 'fn': 'GPIO',
         'pwm': True, 'pwm_chip': '32c0000.pwm', 'pwm_id': 0, 'i2c': None, 'usable': True,
         'notes': 'GPIO35 / PH.00; GPIO by default, hardware PWM capable (32c0000.pwm)'},
    19: {'label': 'SPI1_MOSI', 'role': 'spi', 'gpio': 135, 'chip': 'tegra234-gpio', 'line': 'PZ.05', 'fn': 'SPI1_MOSI',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 MOSI / PZ.05; SFIO by default'},
    20: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    21: {'label': 'SPI1_MISO', 'role': 'spi', 'gpio': 134, 'chip': 'tegra234-gpio', 'line': 'PZ.04', 'fn': 'SPI1_MISO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 MISO / PZ.04; SFIO by default'},
    22: {'label': 'GPIO17', 'role': 'gpio', 'gpio': 96, 'chip': 'tegra234-gpio', 'line': 'PP.04', 'fn': 'GPIO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
         'notes': 'GPIO17 / PP.04; GPIO by default'},
    23: {'label': 'SPI1_CLK', 'role': 'spi', 'gpio': 133, 'chip': 'tegra234-gpio', 'line': 'PZ.03', 'fn': 'SPI1_CLK',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Clock / PZ.03; SFIO by default'},
    24: {'label': 'SPI1_CS0_N', 'role': 'spi', 'gpio': 136, 'chip': 'tegra234-gpio', 'line': 'PZ.06', 'fn': 'SPI1_CS0_N',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Chip Select 0 / PZ.06; SFIO by default'},
    25: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    26: {'label': 'SPI1_CS1_N', 'role': 'spi', 'gpio': 137, 'chip': 'tegra234-gpio', 'line': 'PZ.07', 'fn': 'SPI1_CS1_N',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'SPI1 Chip Select 1 / PZ.07; SFIO by default'},
    27: {'label': 'I2C0_SDA', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C0_SDA', 'fn': 'I2C0_SDA',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 0, 'usable': False,
         'notes': 'ID EEPROM I2C0 SDA; reserved for board identification, never touch'},
    28: {'label': 'I2C0_SCL', 'role': 'i2c', 'gpio': None, 'chip': None, 'line': 'I2C0_SCL', 'fn': 'I2C0_SCL',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': 0, 'usable': False,
         'notes': 'ID EEPROM I2C0 SCL; reserved for board identification, never touch'},
    29: {'label': 'CAN0_DIN', 'role': 'signal', 'gpio': 1, 'chip': 'tegra234-gpio-aon', 'line': 'PAA.01', 'fn': 'CAN0_DIN',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'CAN0 DIN / PAA.01; SFIO by default'},
    30: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    31: {'label': 'CAN0_DOUT', 'role': 'signal', 'gpio': 0, 'chip': 'tegra234-gpio-aon', 'line': 'PAA.00', 'fn': 'CAN0_DOUT',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'CAN0 DOUT / PAA.00; SFIO by default'},
    32: {'label': 'GPIO09', 'role': 'gpio', 'gpio': 8, 'chip': 'tegra234-gpio-aon', 'line': 'PBB.00', 'fn': 'GPIO',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': True,
         'notes': 'GPIO09 / PBB.00; Always-On (AON) GPIO by default'},
    33: {'label': 'CAN1_DOUT', 'role': 'signal', 'gpio': 2, 'chip': 'tegra234-gpio-aon', 'line': 'PAA.02', 'fn': 'CAN1_DOUT',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'CAN1 DOUT / PAA.02; SFIO by default'},
    34: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    35: {'label': 'I2S2_FS', 'role': 'i2s', 'gpio': 53, 'chip': 'tegra234-gpio', 'line': 'PI.02', 'fn': 'I2S2_FS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S2 Frame Sync / PI.02; SFIO by default'},
    36: {'label': 'UART1_CTS', 'role': 'uart', 'gpio': 113, 'chip': 'tegra234-gpio', 'line': 'PR.05', 'fn': 'UART1_CTS',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'UART1 CTS / PR.05; SFIO by default'},
    37: {'label': 'CAN1_DIN', 'role': 'signal', 'gpio': 3, 'chip': 'tegra234-gpio-aon', 'line': 'PAA.03', 'fn': 'CAN1_DIN',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'CAN1 DIN / PAA.03; SFIO by default'},
    38: {'label': 'I2S2_DIN', 'role': 'i2s', 'gpio': 52, 'chip': 'tegra234-gpio', 'line': 'PI.01', 'fn': 'I2S2_DIN',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S2 Data In / PI.01; SFIO by default'},
    39: {'label': 'GND', 'role': 'gnd', 'gpio': None, 'chip': None, 'line': None, 'fn': 'GND',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'Ground; never drive'},
    40: {'label': 'I2S2_DOUT', 'role': 'i2s', 'gpio': 51, 'chip': 'tegra234-gpio', 'line': 'PI.00', 'fn': 'I2S2_DOUT',
         'pwm': False, 'pwm_chip': None, 'pwm_id': None, 'i2c': None, 'usable': False,
         'notes': 'I2S2 Data Out / PI.00; SFIO by default'},
}


def _jetson_agx_orin_header() -> dict[int, dict]:
    """Return full 40-pin J30 header dictionary for Jetson AGX Orin dev kit."""
    pins = {}
    for physical, info in _JETSON_AGX_ORIN_J30.items():
        role = info['role']
        p = _pin(
            physical,
            label=info['label'],
            role=role,
            gpio=info['gpio'],
            exposed=True,
            pwm=info['pwm'],
            reserved=not info['usable'],
            notes=info['notes'],
            functions=[info['fn']] if info['fn'] else (),
        )
        p['gpiochip'] = info['chip']
        p['line_name'] = info['line']
        p['default_function'] = info['fn']
        p['pwm_chip'] = info['pwm_chip']
        p['pwm_id'] = info['pwm_id']
        p['i2c_bus'] = info['i2c']
        p['usable'] = info['usable']
        pins[physical] = p
    return pins


# Exported SBC board profiles
BOARD_PROFILES_SBC: dict[str, dict] = {}

BOARD_PROFILES_SBC['raspberry_pi_4'] = _profile(
    'Raspberry Pi 4 Model B',
    'Raspberry Pi SBC',
    _pi_header('pinctrl-bcm2711'),
    ['rpi', 'rpi4'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin physical header. Broadcom BCM2711 gpiochip (pinctrl-bcm2711). Hardware PWM0 on GPIO 12/18, PWM1 on GPIO 13/19.',
)
BOARD_PROFILES_SBC['raspberry_pi_4']['gpiochip'] = 'pinctrl-bcm2711'

BOARD_PROFILES_SBC['raspberry_pi_5'] = _profile(
    'Raspberry Pi 5',
    'Raspberry Pi SBC',
    _pi_header('pinctrl-rp1'),
    ['rpi', 'rpi5'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin physical header routed via RP1 southbridge (gpiochip: pinctrl-rp1). 4 independent hardware PWM channels.',
)
BOARD_PROFILES_SBC['raspberry_pi_5']['gpiochip'] = 'pinctrl-rp1'

BOARD_PROFILES_SBC['raspberry_pi_zero_2w'] = _profile(
    'Raspberry Pi Zero 2 W',
    'Raspberry Pi SBC',
    _pi_header('pinctrl-bcm2835'),
    ['rpi', 'rpi_zero_2w'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin physical header. Broadcom BCM2837/BCM2710 gpiochip (pinctrl-bcm2835).',
)
BOARD_PROFILES_SBC['raspberry_pi_zero_2w']['gpiochip'] = 'pinctrl-bcm2835'

BOARD_PROFILES_SBC['raspberry_pi_3b_plus'] = _profile(
    'Raspberry Pi 3 Model B+',
    'Raspberry Pi SBC',
    _pi_header('pinctrl-bcm2835'),
    ['rpi'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin physical header. Broadcom BCM2837B0 gpiochip (pinctrl-bcm2835).',
)
BOARD_PROFILES_SBC['raspberry_pi_3b_plus']['gpiochip'] = 'pinctrl-bcm2835'

BOARD_PROFILES_SBC['jetson_orin_nano_devkit'] = _profile(
    'NVIDIA Jetson Orin Nano Developer Kit reference carrier',
    'Jetson SBC',
    _jetson_orin_header(),
    ['jetson', 'jetson_carrier'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin J12 expansion header on P3768 carrier board (tegra234-gpio). Only pins default to GPIO in carrier pinmux are marked usable.',
)
BOARD_PROFILES_SBC['jetson_orin_nano_devkit']['gpiochip'] = 'tegra234-gpio'

BOARD_PROFILES_SBC['jetson_orin_nx_devkit'] = _profile(
    'NVIDIA Jetson Orin NX Developer Kit reference carrier',
    'Jetson SBC',
    _jetson_orin_header(),
    ['jetson', 'jetson_carrier', 'jetson_orin_nx'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin J12 expansion header on P3768 carrier board (tegra234-gpio). Only pins default to GPIO in carrier pinmux are marked usable.',
)
BOARD_PROFILES_SBC['jetson_orin_nx_devkit']['gpiochip'] = 'tegra234-gpio'

BOARD_PROFILES_SBC['jetson_agx_orin_devkit'] = _profile(
    'NVIDIA Jetson AGX Orin Developer Kit reference carrier',
    'Jetson SBC',
    _jetson_agx_orin_header(),
    ['jetson', 'jetson_agx_orin'],
    pin_index='physical_header',
    transport='ssh_agent',
    verified_exposure=True,
    notes='40-pin J30 expansion header on P3737 carrier board (tegra234-gpio, tegra234-gpio-aon). Only pins default to GPIO in carrier pinmux are marked usable.',
)
BOARD_PROFILES_SBC['jetson_agx_orin_devkit']['gpiochip'] = 'tegra234-gpio'

# Common aliases
BOARD_PROFILES_SBC['raspberry_pi_4b'] = BOARD_PROFILES_SBC['raspberry_pi_4']
BOARD_PROFILES_SBC['jetson_orin_nano'] = BOARD_PROFILES_SBC['jetson_orin_nano_devkit']
BOARD_PROFILES_SBC['jetson_orin_nx'] = BOARD_PROFILES_SBC['jetson_orin_nx_devkit']
BOARD_PROFILES_SBC['jetson_agx_orin'] = BOARD_PROFILES_SBC['jetson_agx_orin_devkit']


def detect(model_string: str) -> str | None:
    """Identify the matching profile_id from a /proc/device-tree/model string.

    Examples:
        detect("Raspberry Pi 5 Model B Rev 1.0\\x00") -> "raspberry_pi_5"
        detect("Raspberry Pi 4 Model B Rev 1.4") -> "raspberry_pi_4"
        detect("Raspberry Pi Zero 2 W Rev 1.0") -> "raspberry_pi_zero_2w"
        detect("NVIDIA Jetson Orin Nano Developer Kit") -> "jetson_orin_nano_devkit"
        detect("NVIDIA Jetson Orin NX Developer Kit") -> "jetson_orin_nx_devkit"
        detect("NVIDIA Jetson AGX Orin Developer Kit") -> "jetson_agx_orin_devkit"
    """
    if not model_string or not isinstance(model_string, str):
        return None

    cleaned = model_string.strip('\x00\r\n\t ').strip()
    lowered = cleaned.lower()

    if 'raspberry pi' in lowered:
        if 'zero 2' in lowered:
            return 'raspberry_pi_zero_2w'
        if 'pi 5' in lowered:
            return 'raspberry_pi_5'
        if 'pi 4' in lowered:
            return 'raspberry_pi_4'
        if '3 model b' in lowered or 'pi 3' in lowered:
            return 'raspberry_pi_3b_plus'
    elif 'jetson' in lowered:
        if 'orin nano' in lowered:
            return 'jetson_orin_nano_devkit'
        if 'orin nx' in lowered:
            return 'jetson_orin_nx_devkit'
        if 'agx orin' in lowered:
            return 'jetson_agx_orin_devkit'

    return None


def get_profile(board_id: str) -> dict:
    """Return a complete independent reference pinout for an SBC board ID."""
    if board_id not in BOARD_PROFILES_SBC:
        raise KeyError(f'Unknown SBC board profile: {board_id}')
    return deepcopy(BOARD_PROFILES_SBC[board_id])


def list_board_ids() -> list[str]:
    """Return all known SBC profile IDs sorted."""
    return sorted(BOARD_PROFILES_SBC)


def candidate_gpio(board_id: str) -> dict[int, dict]:
    """List physically exposed GPIO candidates for an SBC board; fail-closed metadata only."""
    profile = get_profile(board_id)
    return {
        n: p for n, p in profile['pins'].items()
        if p.get('exposed') is True and p.get('type') == 'gpio' and not p.get('reserved')
    }


def assert_no_automatic_probe() -> bool:
    """Validate fail-closed security invariant across all SBC profiles."""
    for board_id, profile in BOARD_PROFILES_SBC.items():
        for n, p in profile['pins'].items():
            if any(p.get(key) for key in ('safe_probe', 'safe_read', 'safe_digital_write', 'safe_pwm')):
                raise AssertionError(f'{board_id} pin {n} is unexpectedly auto-approved')
    return True
