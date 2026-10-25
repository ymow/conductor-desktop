"""mobile_type on a composing keyboard layout.

On a Gboard Zhuyin layout, `adb shell input text wifi` exits 0 but the letters
sit in the composition strip ("wifi wifi wifi ㄊ") and never reach the field,
so type_text used to report "typed 4 chars" for text that didn't land. It now
cycles to an ASCII-capable layout first and back afterwards, or warns.

The dumpsys lines are trimmed from a Seeker on Android 16.
"""

from __future__ import annotations

from conductor_desktop.core.backends.mobile import (
    AndroidExecutor,
    _enabled_subtypes,
    _subtype_ascii_capable,
)

GBOARD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
ZHUYIN, ALPHABET, ENGLISH = "-1846817932", "-1097284911", "1594443099"
ENABLED = f"{GBOARD};{ZHUYIN};{ALPHABET};{ENGLISH}:com.google.android.tts/.Voice"

DUMP = "\n".join(
    f"            mSubtypeNameOverride= mSubtypeLocale={loc} mSubtypeMode=keyboard "
    f"mIsAuxiliary=false mOverridesImplicitlyEnabledSubtype=false "
    f"mIsAsciiCapable={ascii_} mSubtypeHashCode={h}"
    for loc, ascii_, h in (("zh_TW", "false", ZHUYIN), ("", "true", ALPHABET),
                           ("en_US", "true", ENGLISH))
)


def test_subtype_ascii_capable_reads_the_dump():
    assert _subtype_ascii_capable(DUMP, ZHUYIN) is False
    assert _subtype_ascii_capable(DUMP, ALPHABET) is True
    assert _subtype_ascii_capable(DUMP, "123") is None
    assert _subtype_ascii_capable(DUMP, "-1") is None


def test_enabled_subtypes_picks_the_active_ime():
    assert _enabled_subtypes(ENABLED, GBOARD) == [ZHUYIN, ALPHABET, ENGLISH]
    assert _enabled_subtypes(ENABLED, "other/.Ime") == []


class Phone:
    """A fake adb: answers the settings/dumpsys reads, cycles the layout on
    KEYCODE_LANGUAGE_SWITCH (unless the keyboard is down), and records what
    was typed and under which layout."""

    def __init__(self, layout: str, keyboard_up: bool = True):
        self.layout, self.keyboard_up = layout, keyboard_up
        self.typed: list[tuple[str, str]] = []
        self.order = [ZHUYIN, ALPHABET, ENGLISH]

    async def adb_text(self, *args: str, timeout: float = 15) -> str:
        cmd = " ".join(args)
        if cmd.endswith("selected_input_method_subtype"):
            return self.layout + "\n"
        if cmd.endswith("default_input_method"):
            return GBOARD + "\n"
        if cmd.endswith("enabled_input_methods"):
            return ENABLED + "\n"
        if "dumpsys input_method" in cmd:
            return DUMP
        if cmd.endswith("KEYCODE_LANGUAGE_SWITCH"):
            if self.keyboard_up:
                i = self.order.index(self.layout)
                self.layout = self.order[(i + 1) % len(self.order)]
            return ""
        if args[1:3] == ("input", "text"):
            self.typed.append((args[3], self.layout))
            return ""
        raise AssertionError(f"unexpected adb call: {cmd}")


def _executor(phone: Phone) -> AndroidExecutor:
    ex = AndroidExecutor("SM02G4061922888")
    ex._adb_text = phone.adb_text
    ex._layout_settle = 0
    return ex


async def test_composing_layout_is_switched_for_the_text_and_restored():
    phone = Phone(ZHUYIN)
    res = await _executor(phone).type_text("wifi")
    assert res.success
    assert phone.typed == [("wifi", ALPHABET)]
    assert phone.layout == ZHUYIN
    assert "switched to an ASCII keyboard layout for this and back" in res.output


async def test_ascii_layout_types_without_switching():
    phone = Phone(ENGLISH)
    res = await _executor(phone).type_text("wifi")
    assert phone.typed == [("wifi", ENGLISH)] and phone.layout == ENGLISH
    assert res.output == "typed 4 chars on SM02G4061922888"


async def test_unswitchable_layout_warns_instead_of_claiming_success():
    phone = Phone(ZHUYIN, keyboard_up=False)
    res = await _executor(phone).type_text("wifi")
    assert phone.typed == [("wifi", ZHUYIN)] and phone.layout == ZHUYIN
    assert "warning" in res.output and "composition strip" in res.output


async def test_non_ascii_text_is_typed_as_before():
    phone = Phone(ZHUYIN)
    await _executor(phone).type_text("中文")
    assert phone.typed == [("'中文'", ZHUYIN)]  # shell-quoted for the device's sh
