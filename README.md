# epro2kicad

Convert **EasyEDA Pro** projects (`.epro`, `.epro2`, `.zip`) into native **KiCad 10** projects: schematic, PCB,
libraries, zones and design rules included. It also adds an LCSC parts browser to KiCad.

No dependencies beyond Python 3 and KiCad 10.

## Install

```sh
git clone https://github.com/filipton/easyeda-to-kicad
cd easyeda-to-kicad
python3 -m epro2kicad.install        # close KiCad first
```

Then in KiCad, enable the API under **Settings → Plugins → Enable KiCad API**.

To uninstall: `python3 -m epro2kicad.install --uninstall`

## Usage

**In KiCad:** click **Import EasyEDA Pro project** in the schematic or PCB editor toolbar.
Press `Shift+A` in the schematic editor to open the LCSC library.

**On macOS:** double-click an `.epro` file, or drop it on **EasyEDA to KiCad.app**.

**From the command line:**

```sh
python3 -m epro2kicad MyProject.epro -o out/       # convert all boards
python3 -m epro2kicad MyProject.epro --list        # list boards
python3 -m epro2kicad MyProject.epro -b Main --3d  # one board, with 3D models
python3 -m epro2kicad.lcsc C2040 C25804            # add LCSC parts to the current project
```

Add `--kicad-symbols` to use KiCad's built-in symbols for standard parts where the netlist stays identical.

Each converted board includes a `conversion-report.md` with the checks that ran.
