#!/usr/bin/env python
# License: GPLv3

# Deterministic payload generators for the measurement driver (spike_driver_lto.py).
# The first eleven are ports of the scenarios in tools/cmd/benchmark/main.go,
# followed by a CJK corpus and the RTL corpora. This is a plain module, not a
# test: the test runner imports every module in kitty_tests/, so nothing here
# may do work at import time.
#
# A generator takes a target size and returns a Payload. The size is the
# number of cells for the line based text corpora, bytes for csi, bytes of OSC
# data for long_escape_codes, pixels for images and codepoints for
# unique_unicode. The same size always gives the same bytes.

import base64
import math
import random
from collections.abc import Callable
from typing import NamedTuple


class Payload(NamedTuple):
    pieces: list[bytes]  # the unit the driver feeds in frames: a line, a row, a chunk or an escape code
    cells: int  # cells drawn, 0 when not meaningful
    main_screen: bool  # the Go benchmark runs only scrollback on the main screen


ASCII_PRINTABLE = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ  `~!@#$%^&*()_+-=[]{}\\|;:\'",<.>/?'
CONTROL_CHARS = '\n\t'
CHINESE_LOREM_IPSUM = (
    '旦海司有幼雞讀松鼻種比門真目怪少：扒裝虎怕您跑綠蝶黃，位香法士錯乙音造活羽詞坡村目園尺封鳥朋；法松夕點我冬停雪因科對只貓息加黃住蝶，明鴨乾春呢風乙時昔孝助？小紅女父故去。'
    '飯躲裝個哥害共買去隻把氣年，己你校跟飛百拉！快石牙飽知唱想土人吹象毛吉每浪四又連見、欠耍外豆雞秋鼻。住步帶。'
    '打六申幾麼：或皮又荷隻乙犬孝習秋還何氣；幾裏活打能花是入海乙山節會。種第共後陽沒喜姐三拍弟海肖，行知走亮包，他字幾，的木卜流旦乙左杯根毛。'
    '您皮買身苦八手牛目地止哥彩第合麻讀午。原朋河乾種果「才波久住這香松」兄主衣快他玉坐要羽和亭但小山吉也吃耳怕，也爪斗斥可害朋許波怎祖葉卜。'
    '行花兩耍許車丟學「示想百吃門高事」不耳見室九星枝買裝，枝十新央發旁品丁青給，科房火；事出出孝肉古：北裝愛升幸百東鼻到從會故北「可休笑物勿三游細斗」娘蛋占犬。我羊波雨跳風。'
    '牛大燈兆新七馬，叫這牙後戶耳、荷北吃穿停植身玩間告或西丟再呢，他禾七愛干寺服石安：他次唱息它坐屋父見這衣發現來，苗會開條弓世者吃英定豆哭；跳風掃叫美神。'
    '寸再了耍休壯植己，燈錯和，蝶幾欠雞定和愛，司紅後弓第樹會金拉快喝夕見往，半瓜日邊出讀雞苦歌許開；發火院爸乙；四帶亮錯鳥洋個讀。'
)
MISC_UNICODE = (
    '‘’“”‹›«»‚„ 😀😛😇😈😉😍😎😮👍👎 —–§¶†‡©®™ →⇒•·°±−×÷¼½½¾…µ¢£€¿¡¨´¸ˆ˜ ÀÁÂÃÄÅÆÇÈÉÊË ÌÍÎÏÐÑÒÓÔÕÖØ ŒŠÙÚÛÜÝŸÞßàá âãäåæçèéêëìíîïðñòóôõöøœš ùúûüýÿþªºαΩ∞ ū̀n̂o᷵H̨a̠b̡͓̐c̡͓̐X̡͓̐'
)
LATIN_WORDS = ['alpha', 'beta', 'gamma', 'delta', 'kitty', 'render', 'cell', 'shape', 'row', 'glyph']
HEBREW_WORDS = ['שלום', 'עולם', 'טרמינל', 'בדיקה', 'ברוכים', 'הבאים', 'שורה', 'תא', 'גופן', 'צבע']
ARABIC_WORDS = ['مرحبا', 'بالعالم', 'محطة', 'اختبار', 'نص', 'سطر', 'خلية', 'خط', 'لون', 'عربي']
ROW_LENGTH = 80  # fits a 120 column screen in one row
WRAPPED_LENGTH = 240  # takes two rows at 120 columns
MAX_BRACKET_DEPTH = 3


def _grouped(units: list[str], per_piece: int) -> list[bytes]:
    # whole units only, so that no piece ends inside an escape code or a character
    return [''.join(units[i : i + per_piece]).encode() for i in range(0, len(units), per_piece)]


def _layout(count: int) -> list[tuple[int, int]]:
    # (width, tab_at) per line as in text_layout() in the Go benchmark: the
    # same lines for ascii and unicode so that they are comparable
    r = random.Random(1)
    ans: list[tuple[int, int]] = []
    total = 0
    while total < count:
        width = r.randint(0, 100)
        tab_at = r.randrange(width) if width > 8 and r.randrange(4) == 0 else -1
        ans.append((width, tab_at))
        total += width
    return ans


def _text_with_layout(count: int, next_char: Callable[[int], tuple[str, int]]) -> Payload:
    # next_char(remaining) returns a character, possibly several codepoints,
    # and its width in cells which is at most remaining
    lines: list[bytes] = []
    cells = 0
    for width, tab_at in _layout(count):
        out: list[str] = []
        tab_done = tab_at < 0
        filled = 0
        while filled < width:
            if not tab_done and filled >= tab_at:
                out.append('\t')
                tab_done = True
            ch, w = next_char((width if tab_done else tab_at) - filled)
            out.append(ch)
            filled += w
        out.append('\r\n')
        lines.append(''.join(out).encode())
        cells += width
    return Payload(lines, cells, False)


def ascii_text(count: int) -> Payload:
    r = random.Random(2)
    return _text_with_layout(count, lambda remaining: (r.choice(ASCII_PRINTABLE), 1))


def scrollback(count: int) -> Payload:
    return ascii_text(count)._replace(main_screen=True)


def unicode_text(count: int) -> Payload:
    from kitty.fast_data_types import split_into_graphemes, wcswidth

    narrow: list[str] = []
    wide: list[str] = []
    for g in split_into_graphemes(CHINESE_LOREM_IPSUM + MISC_UNICODE):
        w = wcswidth(g)
        if w == 1 and g != ' ':
            narrow.append(g)
        elif w == 2:
            wide.append(g)
    r = random.Random(2)

    def next_char(remaining: int) -> tuple[str, int]:
        if remaining > 1 and r.randrange(2) == 0:
            return r.choice(wide), 2
        if r.randrange(16) == 0:
            return ' ', 1
        return r.choice(narrow), 1

    return _text_with_layout(count, next_char)


def unique_unicode(count: int) -> Payload:
    # every cell is an 'a' with three combining marks, all cells distinct
    combining_count = 0x70
    out: list[str] = []
    for i in range(count // 4):
        q = i
        cell = ['a']
        for _ in range(3):
            cell.append(chr(0x300 + q % combining_count))
            q //= combining_count
        out.append(''.join(cell))
    return Payload(_grouped(out, 1024), 0, False)


def csi(count: int) -> Payload:
    r = random.Random(2)
    alphabet = ASCII_PRINTABLE + CONTROL_CHARS
    out: list[str] = []
    size = 0
    while size < count:
        q = r.randrange(100)
        if q < 10:
            chunk = ''.join(r.choices(alphabet, k=r.randrange(72) + 1))
        elif q < 30:
            chunk = '\x1b[m\x1b[?1h\x1b[H'
        elif q < 40:
            chunk = '\x1b[1;2;3;4:3;31m'
        elif q < 50:
            chunk = '\x1b[38:5:24;48:2:125:136:147m'
        elif q < 60:
            chunk = '\x1b[58;5;44;2m'
        elif q < 80:
            chunk = '\x1b[m\x1b[10A\x1b[3E\x1b[2K'
        else:
            chunk = '\x1b[39m\x1b[10`a\x1b[100b\x1b[?1l'
        out.append(chunk)
        size += len(chunk)
    out.append('\x1b[m')
    return Payload(_grouped(out, 64), 0, False)


def long_escape_codes(count: int) -> Payload:
    # OSC 6 is document reporting or XTerm special color which kitty ignores after parsing
    r = random.Random(2)
    data = ''.join(r.choices(ASCII_PRINTABLE, k=8024))
    code = f'\x1b]6;{data}\x07'.encode()
    return Payload([code] * max(1, count // 8024), 0, False)


def _random_color(r: random.Random) -> tuple[int, int, int]:
    # components in [100, 255] so that every color has the same encoded length
    return r.randint(100, 255), r.randint(100, 255), r.randint(100, 255)


def _colored_text(count: int, next_color: Callable[[], tuple[int, int, int]]) -> Payload:
    r = random.Random(3)
    alphabet = ASCII_PRINTABLE + CONTROL_CHARS
    out: list[str] = []
    for _ in range(count):
        out.append('\x1b[38;2;%d;%d;%dm%s' % (*next_color(), r.choice(alphabet)))
    out.append('\x1b[m')
    return Payload(_grouped(out, 128), count, False)


def truecolor_text(count: int) -> Payload:
    # every char has a random 24-bit foreground so nearly every cell has a distinct style
    r = random.Random(2)
    return _colored_text(count, lambda: _random_color(r))


def palette16_text(count: int) -> Payload:
    # the same as truecolor_text with 16 distinct colors, so styles can be reused
    r = random.Random(2)
    colors = [_random_color(r) for _ in range(16)]
    return _colored_text(count, lambda: r.choice(colors))


def halfblocks(count: int) -> Payload:
    # rows of 200 upper half blocks each with random 24-bit foreground and background
    r = random.Random(2)
    rows: list[bytes] = []
    for _ in range(max(1, count // 200)):
        row = ['\x1b[38;2;%d;%d;%d;48;2;%d;%d;%dm▀' % (*_random_color(r), *_random_color(r)) for _ in range(200)]
        rows.append((''.join(row) + '\x1b[m\r\n').encode())
    return Payload(rows, 200 * len(rows), False)


def images(count: int) -> Payload:
    # one RGBA image of about count pixels transmitted in 128KiB base64 chunks, then deleted
    dim = max(1, math.isqrt(count))
    data = base64.b64encode(bytes(4 * dim * dim)).rstrip(b'=')
    size = 128 * 1024
    pieces: list[bytes] = []
    for i in range(0, len(data), size):
        more = 1 if i + size < len(data) else 0
        keys = f'a=t,q=2,f=32,s={dim},v={dim},i=12345,m={more}' if i == 0 else f'a=t,q=2,m={more}'
        pieces.append(b'\x1b_G' + keys.encode() + b';' + data[i : i + size] + b'\x1b\\')
    pieces.append(b'\x1b_Ga=d,q=2,d=I,i=12345\x1b\\')
    return Payload(pieces, 0, False)


def _fill(word_lists: list[list[str]], length: int, n: int) -> str:
    # a line of exactly length characters from alternating word lists, padded
    # with x so that it never ends in a space
    out: list[str] = []
    total = 0
    i = n
    while total < length:
        w = word_lists[i % len(word_lists)][(i // len(word_lists) + n) % 10]
        i += 1
        out.append(w)
        total += len(w) + 1
    ln = ' '.join(out)[:length].rstrip()
    return ln + 'x' * (length - len(ln))


def _lines(count: int, make_line: Callable[[int], str]) -> Payload:
    lines: list[bytes] = []
    total = n = 0
    while total < count:
        ln = make_line(n)
        lines.append((ln + '\r\n').encode())
        total += len(ln)
        n += 1
    return Payload(lines, total, False)


def cjk(count: int) -> Payload:
    # ideographs only, two cells each, in the same line widths as ascii but without tabs
    chars = [c for c in CHINESE_LOREM_IPSUM if '一' <= c <= '鿿']
    r = random.Random(2)
    lines: list[bytes] = []
    cells = 0
    for width, _ in _layout(count):
        lines.append((''.join(r.choices(chars, k=width // 2)) + '\r\n').encode())
        cells += width // 2 * 2
    return Payload(lines, cells, False)


def hebrew(count: int) -> Payload:
    return _lines(count, lambda n: _fill([HEBREW_WORDS], ROW_LENGTH, n))


def arabic(count: int) -> Payload:
    return _lines(count, lambda n: _fill([ARABIC_WORDS], ROW_LENGTH, n))


def mixed_rows(count: int) -> Payload:
    return _lines(count, lambda n: _fill([HEBREW_WORDS, LATIN_WORDS], ROW_LENGTH, n))


def mixed_wrapped(count: int) -> Payload:
    return _lines(count, lambda n: _fill([HEBREW_WORDS, LATIN_WORDS], WRAPPED_LENGTH, n))


_screen = {'cols': 120, 'lines': 50}


def set_screen(cols: int, lines: int) -> None:
    _screen.update(cols=cols, lines=lines)


def para_screen(count: int) -> Payload:
    # count is ignored: the size follows the screen set by set_screen.
    # lines Hebrew history lines, then one soft-wrapped mixed paragraph of
    # (lines - 1) * cols single-cell chars with no trailing newline. The
    # history lines scroll the first row off, so the paragraph ends up on
    # screen rows 1..lines-1.
    cols, lines = _screen['cols'], _screen['lines']
    hist = [(_fill([HEBREW_WORDS], ROW_LENGTH, n) + '\r\n').encode() for n in range(lines)]
    length = (lines - 1) * cols
    para = _fill([HEBREW_WORDS, LATIN_WORDS], length, 0)
    return Payload([*hist, para.encode()], length + lines * ROW_LENGTH, False)


def nested_brackets(count: int) -> Payload:
    openers = '([{<'
    closers = ')]}>'
    r = random.Random(2)

    def make_line(n: int) -> str:
        depth = n % MAX_BRACKET_DEPTH + 1
        kinds = [r.randrange(len(openers)) for _ in range(depth)]
        out = [LATIN_WORDS[n % 10]]
        for d, k in enumerate(kinds):
            words = HEBREW_WORDS if d % 2 == 0 else LATIN_WORDS
            out.append(f'{openers[k]}{words[(n + d) % 10]} {words[(n + d + 3) % 10]}')
        out.append(''.join(closers[k] for k in reversed(kinds)))
        out.append(LATIN_WORDS[(n + 5) % 10])
        ln = ' '.join(out)
        return ln + 'x' * (ROW_LENGTH - len(ln))

    return _lines(count, make_line)


def zw_marks(count: int) -> Payload:
    # lines that start with LRM, RLM or ALM before mixed Latin and Hebrew, interleaved with ZWJ emoji
    # sequences and Latin letters with combining marks
    marks = ['\u200e', '\u200f', '\u061c']
    emoji = ['\U0001f468\u200d\U0001f469\u200d\U0001f467', '\U0001f469\u200d\U0001f4bb', '\U0001f3f3\ufe0f\u200d\U0001f308', '\U0001f468\u200d\U0001f680']
    combining = ['e\u0301', 'a\u0308', 'o\u0302\u0323', 'n\u0303']

    def make_line(n: int) -> str:
        if n % 3 == 0:
            return marks[(n // 3) % 3] + _fill([HEBREW_WORDS, LATIN_WORDS], ROW_LENGTH - 1, n)
        if n % 3 == 1:
            return ' '.join(LATIN_WORDS[(n + i) % 10] + emoji[(n + i) % 4] for i in range(8))
        return ' '.join(LATIN_WORDS[(n + i) % 10][:1] + combining[(n + i) % 4] + LATIN_WORDS[(n + i) % 10][1:] for i in range(10))

    return _lines(count, make_line)

def plain_lines(count: int) -> Payload:
    # 80 printable ASCII characters per line, the control for lrm_lines
    return _lines(count, lambda n: ''.join(chr(33 + (n + i) % 94) for i in range(ROW_LENGTH)))


def lrm_lines(count: int) -> Payload:
    # plain_lines with each line prefixed with LRM, at column 0
    return _lines(count, lambda n: '\u200e' + ''.join(chr(33 + (n + i) % 94) for i in range(ROW_LENGTH)))


PROMPT_GLYPH = '\U000f023a'  # a Nerd Font prompt icon, bidi class L
FSI, LRI, PDI = '\u2068', '\u2066', '\u2069'


def isolate_prompt(count: int) -> Payload:
    # prompt-like rows: <glyph> <space+FSI> then a Hebrew/Latin mix as the typed command. The FSI is stored with
    # the space's cell, as a shell prompt ending in FSI produces.
    def make_line(n: int) -> str:
        head = PROMPT_GLYPH + ' ' + FSI
        return head + _fill([HEBREW_WORDS, LATIN_WORDS], ROW_LENGTH - 2, n)

    return _lines(count, make_line)


def isolate_lri(count: int) -> Payload:
    # rows of 16 LRI..PDI pairs (32 controls, the per-paragraph cap): a Latin char, LRI, two Hebrew
    # letters, PDI, space, five cells per unit and 80 per row
    letters = 'אבגדהוזחטיכלמנסעפצקרשת'

    def make_line(n: int) -> str:
        units = []
        for k in range(16):
            i = (n + k) % len(letters)
            units.append('abcdefghijklmnopqrstuvwxyz'[(n + k) % 26] + LRI + letters[i] + letters[(i + 3) % len(letters)] + PDI + ' ')
        return ''.join(units)

    return _lines(count, make_line)


def fsi_lines(count: int) -> Payload:
    # plain_lines with each line wrapped in an isolate: FSI at column 0, PDI at the end, as a prompt that isolates its
    # command line writes it
    return _lines(count, lambda n: FSI + ''.join(chr(33 + (n + i) % 94) for i in range(ROW_LENGTH)) + PDI)


GENERATORS: dict[str, Callable[[int], Payload]] = {
    'ascii': ascii_text,
    'unicode': unicode_text,
    'scrollback': scrollback,
    'render': ascii_text,
    'unique_unicode': unique_unicode,
    'csi': csi,
    'long_escape_codes': long_escape_codes,
    'truecolor_text': truecolor_text,
    'palette16_text': palette16_text,
    'halfblocks': halfblocks,
    'images': images,
    'cjk': cjk,
    'hebrew': hebrew,
    'arabic': arabic,
    'mixed_rows': mixed_rows,
    'mixed_wrapped': mixed_wrapped,
    'nested_brackets': nested_brackets,
    'para_screen': para_screen,
    'zw_marks': zw_marks,
    'isolate_prompt': isolate_prompt,
    'isolate_lri': isolate_lri,
    'plain_lines': plain_lines,
    'lrm_lines': lrm_lines,
    'fsi_lines': fsi_lines,
}
