"""
Copy the 12x12 Unicode DZK fonts out of the official SPOTLED APK/XAPK into
spotled_ble/fonts/. The fonts belong to the app vendor and are not shipped
in this repository.

    python scripts/extract_fonts.py path/to/SPOTLED_1.9.4.xapk
"""
import io
import sys
import zipfile
from pathlib import Path

DEST = Path(__file__).resolve().parent.parent / "spotled_ble" / "fonts"


def fonts_from_apk(apk: zipfile.ZipFile) -> int:
    n = 0
    for name in apk.namelist():
        if name.startswith("assets/Unicode_12x12") and name.endswith(".DZK"):
            (DEST / Path(name).name).write_bytes(apk.read(name))
            n += 1
    return n


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    DEST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(sys.argv[1]) as z:
        n = fonts_from_apk(z)
        for inner in (m for m in z.namelist() if m.endswith(".apk")):  # XAPK = zip of APKs
            with zipfile.ZipFile(io.BytesIO(z.read(inner))) as apk:
                n += fonts_from_apk(apk)
    if not n:
        sys.exit("no Unicode_12x12*.DZK fonts found in that file")
    print(f"extracted {n} font(s) to {DEST}")


if __name__ == "__main__":
    main()
