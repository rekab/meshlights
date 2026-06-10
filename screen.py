"""SSD1306 OLED helper — minimal init + draw API.

Wiring: VCC, GND, SCL1 (GPIO 3, header pin 5), SDA1 (GPIO 2, header pin 3).
The Pi must have i2c-1 enabled:
    sudo raspi-config nonint do_i2c 0 && sudo reboot
(or uncomment `dtparam=i2c_arm=on` in /boot/firmware/config.txt and reboot).

connect() returns a Screen on success or None if the OLED can't be reached —
caller should treat the screen as optional so the engine/sim still runs
headless when nothing is plugged in.
"""

import sys

try:
    import board
    import busio
    import adafruit_ssd1306
    from PIL import Image, ImageDraw, ImageFont
except ImportError as e:
    print(f"screen deps missing: {e}", file=sys.stderr)
    board = None


# SSD1306 panels come in 128x64 and 128x32; we default to 64. If the panel
# is 32-tall it'll still init, just draw the top half.
DEFAULT_W = 128
DEFAULT_H = 64
DEFAULT_ADDR = 0x3C   # 0x3D is the other common option


def connect(width=DEFAULT_W, height=DEFAULT_H, addr=DEFAULT_ADDR):
    if board is None:
        return None
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
    except Exception as e:
        print(f"i2c bus open failed: {e}  "
              f"(is i2c enabled? `sudo raspi-config nonint do_i2c 0 && sudo reboot`)",
              file=sys.stderr)
        return None
    try:
        oled = adafruit_ssd1306.SSD1306_I2C(width, height, i2c, addr=addr)
    except Exception as e:
        print(f"SSD1306 init failed at 0x{addr:02X}: {e}  "
              f"(check wiring; run `i2cdetect -y 1` to scan)", file=sys.stderr)
        return None
    return Screen(oled, width, height)


class Screen:
    def __init__(self, oled, width, height):
        self.oled = oled
        self.width = width
        self.height = height
        self.font = ImageFont.load_default()
        self._img = Image.new("1", (width, height))
        self._draw = ImageDraw.Draw(self._img)
        self.clear()

    def clear(self):
        self._draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)
        self.oled.fill(0)
        self.oled.show()

    def show_lines(self, lines, line_h=10):
        """Replace screen with `lines` rendered top-to-bottom."""
        self._draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)
        y = 0
        for line in lines:
            if y >= self.height:
                break
            self._draw.text((0, y), str(line), font=self.font, fill=255)
            y += line_h
        self.oled.image(self._img)
        self.oled.show()

    def close(self):
        try:
            self.clear()
        except Exception:
            pass
