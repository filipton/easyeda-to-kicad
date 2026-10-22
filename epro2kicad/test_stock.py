"""Self-check of the stock-symbol pin mapping (needs KiCad's symbol libraries installed).

    python3 -m epro2kicad.test_stock
"""

from types import SimpleNamespace as NS

from . import stock


def part(prefix, pins, **attrs):
    ls = NS(ref_prefix=prefix, pins=[NS(number=n, name=nm) for n, nm in pins])
    return attrs, ls


def check(cat, prefix, pins, want, **attrs):
    a, ls = part(prefix, pins, **attrs)
    a["LCSC Category"] = cat
    got = stock.choose(a, ls)
    assert (got and got[:2] + (got[2],)) == want, f"{cat} {pins}: {got} != {want}"


def main():
    if not stock.stock_symbols("Device"):
        print("KiCad symbol libraries not found: skipped")
        return
    check("Chip Resistor - Surface Mount", "R", [("1", "1"), ("2", "2")], ("Device", "R", {"1": "1", "2": "2"}))
    # LED: KiCad pin 1 = K, 2 = A, so EasyEDA's K on pad 2 must land on KiCad pin 1
    check("LED Indication - Discrete", "LED", [("1", "A"), ("2", "K")], ("Device", "LED", {"1": "2", "2": "1"}))
    # MOSFET with the drain on several pads -> one stacked pin
    check("MOSFETs", "Q", [("1", "S"), ("2", "S"), ("3", "S"), ("4", "G"), ("5", "D"), ("6", "D"), ("7", "D"),
                           ("8", "D")],
          ("Device", "Q_NMOS", {"D": "[5,6,7,8]", "G": "4", "S": "[1,2,3]"}), Type="N-Channel")
    check("Bipolar (BJT)", "Q", [("1", "B"), ("2", "E"), ("3", "C")],
          ("Device", "Q_PNP", {"B": "1", "C": "3", "E": "2"}), type="PNP")
    # SOT-23-5 op-amp with its own pinout onto KiCad's LM321 drawing
    check("Operational Amplifier", "U", [("1", "OUT"), ("2", "V-"), ("3", "+IN"), ("4", "-IN"), ("5", "V+")],
          ("Amplifier_Operational", "LM321", {"1": "3", "2": "2", "3": "4", "4": "1", "5": "5"}))
    check("Bridge Rectifiers", "D", [("1", "AC"), ("2", "AC"), ("3", "+"), ("4", "-")],
          ("Device", "D_Bridge_+-AA", {"1": "3", "2": "4", "3": "1", "4": "2"}))
    check("Wire To Board Connector", "CN", [("1", "1"), ("2", "2"), ("3", "3"), ("4", "MP"), ("5", "MP")],
          ("Connector_Generic_MountingPin", "Conn_01x03_MountingPin", {"1": "1", "2": "2", "3": "3", "MP": "[4,5]"}))
    check("Limit Switches", "SW", [("1", "COM"), ("2", "NO"), ("3", "NC")],
          ("Switch", "SW_SPDT", {"1": "2", "2": "1", "3": "3"}))
    # a pin we can't identify keeps EasyEDA's symbol
    a, ls = part("Q", [("1", "G"), ("2", "S"), ("3", "KS"), ("4", "D")], Type="N-Channel")
    a["LCSC Category"] = "MOSFETs"
    assert stock.choose(a, ls) is None
    # electrolytic without names or a drawn "+": polarity unknown -> EasyEDA's symbol
    a, ls = part("C", [("1", "1"), ("2", "2")])
    a["LCSC Category"] = "Aluminum Electrolytic Capacitors - SMD"
    assert stock.choose(a, ls) is None
    print("stock symbol mapping: ok")


if __name__ == "__main__":
    main()
