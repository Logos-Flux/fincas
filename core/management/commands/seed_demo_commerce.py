"""Seed a demo company's commerce: a full catalog and 90 days of trading.

    manage.py seed_demo_commerce            # needs DEMO_USERNAMES (a demo deployment)
    manage.py seed_demo_commerce --force    # a throwaway copy without a demo login
    manage.py seed_demo_commerce --company <uuid>

Builds on ``seed_demo_data`` (company, warehouse, the two selling price lists,
the chart of accounts and posting rules; run that first) and adds, all of it
invented:

- **Catalog:** 8 brands and 10 categories (some nested) with descriptions and
  SEO copy; 41 products (the base seed's four among them, filled in where
  blank) with summaries, descriptions, specifications, SEO fields and 1 to 4
  variants each (option values on the multi-variant ones); retail and
  wholesale price-list rows with quantity breaks; supplier costs; stock in
  two warehouses (``WH-MAIN`` and ``WH-EAST``), mostly in stock, some below
  the reorder level, a few out of stock or on backorder. Products,
  categories and brands get image ids under ``demo/`` (``image_id``); they
  show only where ``BUNNY_IMAGES_BASE`` points at a host that has the files.
- **Parties:** 20 business customers (each with a contact, billing and
  shipping addresses, a pricing tier and payment terms) and 6 suppliers.
- **Trading over the last 90 days:** quotes at every stage, each on its CRM
  engagement (draft, sent, negotiating, lost, expired; accepted ones are
  converted to orders through ``orders.convert``); sales orders in every
  status with shipment groups, a few with pick tickets; invoices sent,
  overdue, partly paid and paid, with payments recorded through
  ``payments.services.record_payment``; purchase orders from draft to
  closed, received through ``purchasing.services.receive_purchase_order``
  (which raises the stock).

Every invoice, payment and receipt is posted by the company's posting rules,
exactly as the back office posts them; nothing writes a journal row itself.

**Dates.** Everything is placed relative to the demo clock's anchor
(``core.demo_clock``; today when none is stored), at least one day back, so
``demo_roll_dates`` keeps the data current. Each step runs with
``django.utils.timezone.now`` pinned to its moment (the
``core.release_test.frozen_now`` technique), so the services date their own
rows: ``created_at``, engagement history, receipts and journal entries all
read the step's time. The two places that read ``date.today()``
(``convert_order_to_invoice``'s invoice date and the paid date set on
settlement) are corrected with ``QuerySet.update()`` before anything posts
from them. Steps run in date order across all documents, so document numbers
rise with their dates. Everything runs inside ``suppressed()`` (no lifecycle
signals, no outbox rows) and one transaction: a failure writes nothing.

**Idempotent.** Catalog and parties are matched by natural key (SKU, title,
email, supplier code) and only created or filled in where blank. Each
document chain carries ``metadata.seed_index`` on its first document and is
skipped when that exists, so a second run adds nothing.

Refuses unless ``DEMO_USERNAMES`` is set or ``--force`` is passed, like
``seed_my_day``: it writes fake customers, orders and books into the company.

Self-contained on purpose (only Django and golden's own modules): the public
demo loads it by piping this file into ``manage.py shell`` on a release that
predates the command (``deploy/fly/OUR-DEMO.md``).
"""

from __future__ import annotations

import datetime
import heapq
import random
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

SEED_TAG = "seed_demo_commerce"
IMPORT_SOURCE = "import"  # crm EngagementStageChange.Source.IMPORT: reconstructed history
RNG_SEED = "fincas-demo-commerce"
ZERO = Decimal("0")

# --- the catalog ------------------------------------------------------------------------

#: key: (title, description, website host, featured)
BRANDS = {
    "apex": ("Apex Dynamics", "Industrial electromechanical components and automation systems: servo "
             "and stepper motion, controllers, I/O and operator panels.", True),
    "vanguard": ("Vanguard Precision", "High-precision instrumentation, sensors, and structural hardware "
                 "for test, measurement and process control.", True),
    "ironwood": ("Ironwood Workholding", "Fasteners, framing and workholding for machine shops and "
                 "assembly lines, built to tight tolerances.", False),
    "cobalt": ("Cobalt Ridge Electric", "Power distribution for control panels: DIN-rail supplies, "
               "breakers, enclosures and surge protection.", True),
    "northfield": ("Northfield Fluid Systems", "Valves, pumps, gauges and fittings for water, air and "
                   "light process duty.", False),
    "lumen": ("Lumen Bay Lighting", "Efficient LED lighting for warehouses, plants and hazardous "
              "locations.", False),
    "halcyon": ("Halcyon Safety Supply", "Personal protective equipment and workplace safety stations "
                "that meet the common US standards.", False),
    "tidewater": ("Tidewater Packaging", "Shipping cartons, stretch film and strapping for fulfillment "
                  "and warehouse teams.", False),
}

#: key: (title, parent key, short description, featured)
CATEGORIES = {
    "control": ("Control Electronics", None,
                "Programmable logic controllers, actuators, and power modules.", True),
    "motion": ("Motion Control", "control", "Servo and stepper motors, drives and linear actuators.", True),
    "sensors": ("Sensors & Measurement", "control",
                "Proximity, pressure, temperature and load sensing.", False),
    "hardware": ("Industrial Hardware", None,
                 "Brackets, fasteners, and heavy-duty structural mountings.", True),
    "fasteners": ("Fasteners & Framing", "hardware", "Cap screws, T-slot extrusion and leveling feet.", False),
    "power": ("Power Distribution", None, "Power supplies, breakers, terminals and enclosures.", True),
    "fluid": ("Fluid Handling", None, "Valves, pumps, gauges and push-to-connect fittings.", False),
    "lighting": ("Industrial Lighting", None, "High bays, linear fixtures and task lights.", False),
    "safety": ("Safety & PPE", None, "Gloves, eyewear, hi-vis and first aid.", False),
    "packaging": ("Packaging Supplies", None, "Cartons, stretch film, strapping and protective bags.", False),
}

#: Who supplies each brand (supplier code), and the suppliers themselves.
BRAND_SUPPLIER = {
    "apex": "SUPP-101", "vanguard": "SUPP-101", "ironwood": "SUPP-102", "cobalt": "SUPP-103",
    "northfield": "SUPP-104", "lumen": "SUPP-105", "halcyon": "SUPP-106", "tidewater": "SUPP-106",
}
SUPPLIERS = (
    # code, company, display, type, contact, city, state, terms, lead days
    ("SUPP-101", "Great Lakes Industrial Distribution", "Great Lakes Industrial", "distributor",
     "Nora Lindqvist", "Milwaukee", "WI", "Net 30", 5),
    ("SUPP-102", "Pinecrest Components", "Pinecrest", "manufacturer", "Owen Castillo", "Grand Rapids", "MI",
     "Net 45", 12),
    ("SUPP-103", "Bayview Electrical Wholesale", "Bayview Electrical", "distributor", "Priya Raman",
     "Toledo", "OH", "Net 30", 4),
    ("SUPP-104", "Summit Fluid Power", "Summit Fluid", "distributor", "Caleb Brooks", "Pittsburgh", "PA",
     "Net 30", 7),
    ("SUPP-105", "Brightway Lighting Supply", "Brightway", "manufacturer", "Mei Tanaka", "Columbus", "OH",
     "Net 60", 14),
    ("SUPP-106", "Keel & Anchor Supply Co.", "Keel & Anchor", "distributor", "Rosa Delgado", "Baltimore", "MD",
     "Net 15", 3),
)


def P(title, brand, cat, part, price, summary, specs, option=None, variants=None, stock="ok", msrp=None,
      sale=None, featured=False):
    """One catalog product. ``variants``: ((value, sku suffix, retail), ...) under ``option``."""
    return {"title": title, "brand": brand, "cat": cat, "part": part, "price": Decimal(price),
            "summary": summary, "specs": specs, "option": option, "variants": variants or (),
            "stock": stock, "msrp": Decimal(msrp) if msrp else None, "sale": Decimal(sale) if sale else None,
            "featured": featured}


PRODUCTS = (
    # The base seed's four (matched by title and SKU; blanks are filled in).
    P("Industrial High-Torque Servo Actuator 500W", "apex", "motion", "ACT-500W", "389.00",
      "A 500 W AC servo actuator with a 17-bit absolute encoder, for indexing tables, conveyors and "
      "pick-and-place axes.",
      {"Rated power": "500 W", "Rated torque": "1.6 Nm", "Peak torque": "4.8 Nm", "Encoder": "17-bit absolute",
       "Voltage": "200-240 VAC", "Protection": "IP65"}, featured=True),
    P("Modular Logic Controller Core Module", "apex", "control", "MLC-CORE-08", "649.50",
      "The CPU module of the modular controller family: 8 local I/O slots, Ethernet/IP and Modbus TCP, "
      "and a free programming suite.",
      {"Program memory": "2 MB", "I/O slots": "8", "Networks": "EtherNet/IP, Modbus TCP",
       "Supply": "24 VDC", "Mounting": "DIN rail"}, featured=True),
    P("Reinforced Structural Corner Bracket 90-Deg", "vanguard", "hardware", "BRK-90-HD", "24.95",
      "A zinc-plated steel corner bracket with a reinforcing gusset, rated for 600 lb shelf and frame loads.",
      {"Material": "Steel, zinc plated", "Thickness": "3/16 in", "Leg length": "4 in", "Load rating": "600 lb"}),
    P("Ultra-Precision Laser Measurement Sensor", "vanguard", "sensors", "SNR-LASER-30", "895.00",
      "A triangulation laser sensor with 2 micron repeatability over a 30 mm range, analog and IO-Link "
      "outputs.",
      {"Range": "30 mm", "Repeatability": "2 um", "Outputs": "4-20 mA, IO-Link", "Laser class": "2",
       "Protection": "IP67"}, stock="low"),
    # Apex Dynamics
    P("Compact Stepper Motor NEMA 23", "apex", "motion", "STP-23", "79.00",
      "A two-phase hybrid stepper in a NEMA 23 frame, with an 8 mm shaft and a 1.8 degree step angle.",
      {"Frame": "NEMA 23", "Step angle": "1.8 deg", "Shaft": "8 mm", "Rated current": "4.2 A"},
      option="Holding torque", variants=(("1.9 Nm", "19", "79.00"), ("2.8 Nm", "28", "96.00"))),
    P("Single-Axis Servo Drive 1 kW", "apex", "motion", "SDR-1K", "612.00",
      "A 1 kW servo drive with auto-tuning, safe torque off and an EtherCAT interface; pairs with the "
      "500 W and 1 kW actuators.",
      {"Output": "1 kW", "Interface": "EtherCAT", "Safety": "STO (SIL 3)", "Input": "200-240 VAC"},
      stock="back"),
    P("Digital I/O Expansion Module", "apex", "control", "DIO-16", "142.00",
      "A 16-point digital I/O module for the modular controller, with per-channel status LEDs and a "
      "removable terminal block.",
      {"Points": "16", "Voltage": "24 VDC", "Isolation": "500 V"},
      option="Configuration", variants=(("16 inputs", "IN", "142.00"), ("16 outputs", "OUT", "156.00"),
                                        ("8 in / 8 out", "MIX", "149.00"))),
    P("HMI Touch Panel 7-inch", "apex", "control", "HMI-07", "468.00",
      "A 7-inch widescreen operator panel with a resistive touch screen, recipe storage and remote access.",
      {"Display": "7 in, 800 x 480", "Touch": "Resistive", "Ports": "Ethernet, RS-485, USB",
       "Front rating": "IP66"}, msrp="520.00"),
    P("Electric Linear Actuator 24 V", "apex", "motion", "LIN-ACT", "214.00",
      "A ball-screw linear actuator with built-in limit switches and position feedback, for gates, "
      "dampers and light presses.",
      {"Force": "1500 N", "Speed": "12 mm/s", "Voltage": "24 VDC", "Feedback": "Hall sensor"},
      option="Stroke", variants=(("100 mm", "100", "214.00"), ("200 mm", "200", "231.00"),
                                 ("300 mm", "300", "249.00"))),
    # Vanguard Precision
    P("Inductive Proximity Sensor M18", "vanguard", "sensors", "PRX-M18", "38.50",
      "A flush-mount M18 inductive sensor with an 8 mm sensing range, an M12 connector and a nickel-plated "
      "brass body.",
      {"Sensing range": "8 mm", "Housing": "M18, nickel-plated brass", "Connection": "M12, 4-pin",
       "Supply": "10-30 VDC"},
      option="Output", variants=(("PNP", "PNP", "38.50"), ("NPN", "NPN", "38.50"))),
    P("Pressure Transmitter 4-20 mA", "vanguard", "sensors", "PTX", "189.00",
      "A stainless pressure transmitter with 0.25% accuracy and a 1/4 in NPT process connection.",
      {"Accuracy": "0.25% FS", "Output": "4-20 mA", "Process connection": "1/4 in NPT",
       "Wetted parts": "316L stainless"},
      option="Range", variants=(("0-100 psi", "100", "189.00"), ("0-300 psi", "300", "189.00"),
                                ("0-1000 psi", "1000", "214.00"))),
    P("RTD Temperature Probe PT100", "vanguard", "sensors", "RTD-PT100", "64.00",
      "A three-wire PT100 probe in a 1/4 in stainless sheath, Class A, with a compression fitting.",
      {"Element": "PT100, Class A", "Sheath": "1/4 in 316 stainless", "Wiring": "3-wire",
       "Range": "-50 to 400 C"},
      option="Length", variants=(("4 in", "4", "64.00"), ("6 in", "6", "69.00"), ("12 in", "12", "82.00"))),
    P("S-Type Load Cell 500 lb", "vanguard", "sensors", "LC-S-500", "176.00",
      "An alloy-steel S-beam load cell for tension and compression, with a 3 mV/V output.",
      {"Capacity": "500 lb", "Output": "3 mV/V", "Material": "Alloy steel", "Protection": "IP67"},
      stock="out"),
    # Ironwood Workholding
    P("Stainless Hex Cap Screw Pack (50)", "ironwood", "fasteners", "HCS-SS", "18.90",
      "A pack of 50 metric hex cap screws in 18-8 stainless, fully threaded, for frames and fixtures.",
      {"Material": "18-8 stainless", "Thread": "Metric coarse", "Pack": "50"},
      option="Size", variants=(("M6 x 20 mm", "M6", "18.90"), ("M8 x 25 mm", "M8", "26.40"),
                               ("M10 x 30 mm", "M10", "39.80"))),
    P("T-Slot Aluminum Extrusion 40x40", "ironwood", "fasteners", "TSL-4040", "32.00",
      "A 40 x 40 mm aluminum profile with 8 mm slots, clear anodized, cut to length.",
      {"Profile": "40 x 40 mm", "Slot": "8 mm", "Alloy": "6063-T5", "Finish": "Clear anodized"},
      option="Length", variants=(("1 m", "1M", "32.00"), ("2 m", "2M", "58.00"), ("3 m", "3M", "84.00")),
      featured=True),
    P("Heavy-Duty Leveling Feet M12 (4-pack)", "ironwood", "fasteners", "LVF-M12", "44.00",
      "Swivel leveling feet with a 80 mm base and a non-slip pad, rated 1,100 lb each.",
      {"Thread": "M12", "Base": "80 mm", "Load rating": "1,100 lb each", "Pack": "4"}),
    P("Precision Machine Vise 6-inch", "ironwood", "hardware", "VSE-6", "329.00",
      "A ductile-iron machine vise with hardened, ground jaws and 0.0005 in parallelism.",
      {"Jaw width": "6 in", "Opening": "6.5 in", "Clamping force": "8,800 lb", "Weight": "47 lb"},
      sale="299.00"),
    P("Horizontal Toggle Clamp 225 lb", "ironwood", "hardware", "TGC-225", "21.50",
      "A horizontal hold-down toggle clamp with a flanged base and an adjustable rubber spindle.",
      {"Holding capacity": "225 lb", "Base": "Flanged", "Finish": "Zinc plated"}),
    # Cobalt Ridge Electric
    P("DIN-Rail Power Supply 24 V", "cobalt", "power", "PSU-24", "88.00",
      "A slim 24 VDC power supply with an adjustable output, a DC-OK relay and a 150% power boost.",
      {"Output": "24 VDC, adjustable", "Input": "100-240 VAC", "Efficiency": "Up to 93%", "Mounting": "DIN rail"},
      option="Power", variants=(("120 W", "120", "88.00"), ("240 W", "240", "132.50"),
                                ("480 W", "480", "236.00")), featured=True),
    P("Miniature Circuit Breaker 1-Pole", "cobalt", "power", "MCB-1P", "14.20",
      "A C-curve miniature breaker for control circuits, 10 kA interrupting capacity.",
      {"Poles": "1", "Curve": "C", "Interrupting rating": "10 kA", "Mounting": "DIN rail"},
      option="Rating", variants=(("10 A", "10", "14.20"), ("16 A", "16", "14.20"), ("20 A", "20", "15.60"))),
    P("NEMA 4X Polycarbonate Enclosure", "cobalt", "power", "ENC-4X", "96.00",
      "A UV-stabilized polycarbonate enclosure with a hinged clear cover and a removable back panel.",
      {"Rating": "NEMA 4X, IP66", "Material": "Polycarbonate", "Cover": "Hinged, clear"},
      option="Size", variants=(("8 x 6 x 4 in", "864", "96.00"), ("12 x 10 x 6 in", "12106", "148.00"),
                               ("16 x 14 x 8 in", "16148", "219.00"))),
    P("Three-Phase Surge Protective Device", "cobalt", "power", "SPD-3P", "274.00",
      "A Type 2 surge protective device for 480 V three-phase panels, with remote status contacts.",
      {"System": "480Y/277 V, 3-phase", "Type": "2", "Surge current": "50 kA per phase"}, stock="low"),
    P("Terminal Block Kit (100 pieces)", "cobalt", "power", "TB-KIT-100", "59.00",
      "A starter kit of 100 spring-clamp terminal blocks, end plates, jumpers and markers.",
      {"Wire range": "26-12 AWG", "Pieces": "100", "Type": "Spring clamp"}),
    # Northfield Fluid Systems
    P("Brass Ball Valve, Full Port", "northfield", "fluid", "BV-BR", "22.80",
      "A lead-free brass ball valve with a full port, a blowout-proof stem and a lever handle.",
      {"Body": "Lead-free brass", "Pressure": "600 psi WOG", "Ends": "FNPT", "Port": "Full"},
      option="Size", variants=(("1/2 in", "050", "22.80"), ("3/4 in", "075", "31.40"), ("1 in", "100", "46.90"))),
    P("Diaphragm Pump 12 V", "northfield", "fluid", "DPM-12", "118.00",
      "A self-priming diaphragm pump for water and mild chemicals, with a pressure switch and thermal "
      "protection.",
      {"Flow": "4.5 gpm", "Max pressure": "60 psi", "Voltage": "12 VDC", "Ports": "1/2 in"}),
    P("Push-to-Connect Fittings Assortment", "northfield", "fluid", "PTC-ASSORT", "67.50",
      "120 nickel-plated push-to-connect fittings in a labeled case: straights, elbows, tees and plugs.",
      {"Tube sizes": "1/4, 5/16, 3/8 in", "Pieces": "120", "Max pressure": "150 psi"}),
    P("Stainless Pressure Gauge 2.5-inch", "northfield", "fluid", "PG-25", "27.40",
      "A glycerin-filled stainless gauge with a bottom 1/4 in NPT connection.",
      {"Dial": "2.5 in", "Connection": "1/4 in NPT, bottom", "Case": "304 stainless, liquid filled"},
      option="Range", variants=(("0-60 psi", "60", "27.40"), ("0-160 psi", "160", "27.40"))),
    P("2-Way Solenoid Valve 24 VDC", "northfield", "fluid", "SOL-2W", "73.00",
      "A normally closed brass solenoid valve with a Viton seal and a DIN connector coil.",
      {"Function": "2-way, normally closed", "Ports": "1/2 in NPT", "Coil": "24 VDC", "Seal": "Viton"},
      stock="out"),
    # Lumen Bay Lighting
    P("LED High Bay Fixture", "lumen", "lighting", "HB-LED", "189.00",
      "A round UFO-style high bay with a 0-10 V dimmer, 5000 K light and a 10-year driver.",
      {"Color temperature": "5000 K", "Dimming": "0-10 V", "Mounting": "Hook or pendant", "Rating": "IP65"},
      option="Wattage", variants=(("150 W", "150", "189.00"), ("200 W", "200", "229.00")), featured=True),
    P("LED Linear Strip Fixture 4 ft", "lumen", "lighting", "LIN-4FT", "54.00",
      "A 4 ft linkable strip fixture for shops and storage rooms, 5,000 lumens.",
      {"Length": "4 ft", "Output": "5,000 lm", "Color temperature": "4000 K", "Linkable": "Up to 8"}),
    P("Explosion-Proof LED Work Light", "lumen", "lighting", "EXP-WL", "642.00",
      "A Class I Division 1 LED work light with a 50 ft cord and a swivel stand.",
      {"Rating": "Class I Div 1", "Output": "6,500 lm", "Cord": "50 ft"}),
    P("Magnetic-Base Machine Task Light", "lumen", "lighting", "MTL-MAG", "92.00",
      "A flexible-arm LED task light with a 90 lb magnetic base, for lathes and mills.",
      {"Arm": "24 in flexible", "Base": "Magnetic, 90 lb pull", "Voltage": "24 VAC/DC"}),
    # Halcyon Safety Supply
    P("Cut-Resistant Work Gloves A4 (12 pairs)", "halcyon", "safety", "GLV-A4", "58.00",
      "ANSI A4 cut-resistant gloves with a nitrile foam palm, sold by the dozen.",
      {"Cut level": "ANSI A4", "Coating": "Nitrile foam", "Pack": "12 pairs"},
      option="Size", variants=(("Small", "S", "58.00"), ("Medium", "M", "58.00"), ("Large", "L", "58.00"),
                               ("X-Large", "XL", "61.00"))),
    P("Anti-Fog Safety Glasses (12-pack)", "halcyon", "safety", "SG-AF-12", "36.00",
      "Wraparound safety glasses with an anti-fog, anti-scratch coating; Z87.1+ rated.",
      {"Standard": "ANSI Z87.1+", "Lens": "Clear, anti-fog", "Pack": "12"}),
    P("Hi-Visibility Safety Vest Class 2", "halcyon", "safety", "HV-C2", "12.40",
      "A Class 2 mesh safety vest with a zipper front and four pockets.",
      {"Standard": "ANSI 107 Class 2", "Color": "Lime", "Closure": "Zipper"},
      option="Size", variants=(("Medium", "M", "12.40"), ("Large", "L", "12.40"), ("X-Large", "XL", "13.20"))),
    P("First Aid Station, 100-Person", "halcyon", "safety", "FA-100", "158.00",
      "A wall-mount first aid cabinet stocked for 100 people, meeting ANSI Z308.1 Class B.",
      {"Standard": "ANSI Z308.1 Class B", "Capacity": "100 people", "Cabinet": "Metal, wall mount"}),
    P("Lockout/Tagout Kit", "halcyon", "safety", "LOTO-KIT", "129.00",
      "A portable lockout kit with 8 padlocks, hasps, breaker locks, valve covers and tags.",
      {"Padlocks": "8, keyed different", "Contents": "36 pieces", "Case": "Carry bag"}, stock="low"),
    # Tidewater Packaging
    P("Corrugated Shipping Boxes (25-pack)", "tidewater", "packaging", "BOX", "34.00",
      "Single-wall 32 ECT shipping cartons, shipped flat.",
      {"Board": "32 ECT, single wall", "Pack": "25", "Color": "Kraft"},
      option="Size", variants=(("12 x 12 x 12 in", "121212", "34.00"), ("18 x 12 x 12 in", "181212", "41.00"),
                               ("24 x 18 x 18 in", "241818", "63.00"))),
    P("Stretch Wrap Film 18 in x 1500 ft (4 rolls)", "tidewater", "packaging", "SW-18", "72.00",
      "80-gauge cast stretch film for hand wrapping pallets, four rolls per case.",
      {"Width": "18 in", "Length": "1,500 ft", "Gauge": "80", "Rolls": "4"}),
    P("Polyester Pallet Strapping Kit", "tidewater", "packaging", "PSK-1", "119.00",
      "A strapping cart kit with a 1/2 in polyester coil, a tensioner, a sealer and 1,000 seals.",
      {"Strap": "1/2 in polyester, 2,900 ft", "Break strength": "600 lb", "Seals": "1,000"}),
    P("Anti-Static Bubble Bags (100-pack)", "tidewater", "packaging", "ASB", "24.00",
      "Pink anti-static bubble bags with a self-seal flap, for electronics shipments.",
      {"Material": "Anti-static polyethylene", "Closure": "Self-seal", "Pack": "100"},
      option="Size", variants=(("4 x 6 in", "46", "24.00"), ("6 x 8 in", "68", "29.00"), ("8 x 10 in", "810", "35.00"))),
)

DESCRIPTION_TAIL = {
    "control": "Stocked in Chicago and Harrisburg, with programming help from our applications team.",
    "motion": "Sized and configured on request; ask for a motion sizing sheet with your quote.",
    "sensors": "Calibration certificates are available on request.",
    "hardware": "Bulk and cut-to-order quantities ship from Chicago.",
    "fasteners": "Bulk and cut-to-order quantities ship from Chicago.",
    "power": "UL-recognized components, stocked for same-day shipping.",
    "fluid": "Pressure-tested before shipment; larger sizes are quoted on request.",
    "lighting": "Photometric files and rebate paperwork are available for every fixture.",
    "safety": "Volume pricing applies from ten cases.",
    "packaging": "Pallet quantities ship direct from the mill at the price-break rate.",
}

#: (company name, first, last, tier, payment terms, street, city, state, zip, area code,
#:  tax-exempt reason or "", rep index)
CUSTOMERS = (
    ("Harborline Machine Works", "Elena", "Brandt", "wholesale", "Net 30", "1180 Wharf Road", "Erie", "PA", "16507",
     "814", "", 0),
    ("Prairie Gate Fabrication", "Marcus", "Holloway", "wholesale", "Net 30", "455 Mill Street", "Peoria", "IL", "61602",
     "309", "", 1),
    ("Bluestem Packaging Co.", "Grace", "Nakamura", "wholesale", "Net 15", "72 Commerce Drive", "Wichita", "KS", "67202",
     "316", "", 2),
    ("Summit Ridge Facilities", "Daniel", "Okoro", "retail", "Due on Receipt", "9 Ridge Park", "Denver", "CO", "80202",
     "303", "", 3),
    ("Copperleaf Brewing", "Hannah", "Weiss", "retail", "Prepaid", "301 Barrel Lane", "Asheville", "NC", "28801",
     "828", "", 0),
    ("Riverbend Water Authority", "Tomas", "Alvarez", "wholesale", "Net 45", "1 Riverbend Plaza", "Dayton", "OH",
     "45402", "937", "government", 1),
    ("Kestrel Aerospace Components", "Priya", "Sandoval", "wholesale", "Net 30", "2200 Hangar Way", "Wichita", "KS",
     "67209", "316", "", 2),
    ("Oak Hollow School District", "Megan", "Fitzgerald", "retail", "Net 30", "50 Schoolhouse Road", "Lancaster", "PA",
     "17601", "717", "education", 3),
    ("Granite Peak Construction", "Victor", "Lindgren", "wholesale", "Net 30", "880 Quarry Road", "Boise", "ID",
     "83702", "208", "", 0),
    ("Lakeshore Cold Storage", "Aisha", "Rahman", "wholesale", "Net 30", "15 Pier Avenue", "Duluth", "MN", "55802",
     "218", "", 1),
    ("Redwood Valley Farms", "Carlos", "Mendes", "retail", "Due on Receipt", "4100 Orchard Road", "Fresno", "CA",
     "93721", "559", "agricultural", 2),
    ("Meadowlark Robotics", "Sophie", "Laurent", "wholesale", "Net 30", "600 Innovation Way", "Pittsburgh", "PA",
     "15213", "412", "", 3),
    ("Northgate Medical Devices", "Ravi", "Iyer", "wholesale", "Net 45", "27 Northgate Court", "Minneapolis", "MN",
     "55401", "612", "", 0),
    ("Silverline Auto Repair", "Jake", "Morrison", "retail", "Due on Receipt", "1420 Route 9", "Albany", "NY", "12207",
     "518", "", 1),
    ("Foxglove Print Shop", "Lily", "Chen", "retail", "Prepaid", "88 Press Street", "Burlington", "VT", "05401",
     "802", "", 2),
    ("Ironbridge Rail Services", "Samuel", "Achebe", "wholesale", "Net 30", "3 Depot Square", "Altoona", "PA",
     "16601", "814", "", 3),
    ("Clearwater Marina", "Olivia", "Grant", "retail", "Due on Receipt", "12 Harbor Point", "Sarasota", "FL",
     "34236", "941", "", 0),
    ("Thistle & Pine Furniture", "Ethan", "MacLeod", "wholesale", "Net 15", "740 Joinery Lane", "High Point", "NC",
     "27260", "336", "", 1),
    ("Bright Harbor Logistics", "Nadia", "Petrova", "wholesale", "Net 30", "5000 Freight Parkway", "Columbus", "OH",
     "43215", "614", "", 2),
    ("Juniper Lane Bakery Equipment", "Owen", "Fletcher", "wholesale", "Net 30", "19 Juniper Lane", "Madison", "WI",
     "53703", "608", "", 3),
)

REPS = ("Ana Reyes", "Ben Okafor", "Chloe Martin", "Dev Patel")  # seed_crm's reps, matched by name

# --- helpers ----------------------------------------------------------------------------


def q2(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _domain(company_name):
    return slugify(company_name.replace("&", "and")).replace("-co", "") or "customer"


def variant_rows(spec):
    """``[(sku, name, retail, option value)]`` for a catalog product."""
    if not spec["variants"]:
        return [(spec["part"], spec["title"], spec["price"], None)]
    return [(f"{spec['part']}-{suffix}", value, Decimal(price), value) for value, suffix, price in spec["variants"]]


def image_id(kind, name):
    """A demo image id (``demo/<kind>/<name>``). Without ``BUNNY_IMAGES_BASE``
    no URL is built and the storefront shows its placeholder; with it, the
    file must exist at ``<BUNNY_IMAGES_BASE>/demo__<kind>__<name>`` (the CDN
    key flattens ``/`` to ``__``, products.templatetags.bunny_images)."""
    value = f"demo/{kind}/{name}"
    return value if len(value) <= 64 else ""  # the image id columns are 64 long


def wholesale_of(retail):
    return q2(retail * Decimal("0.72"))


def cost_of(retail):
    return q2(retail * Decimal("0.55"))


def retail_breaks(retail):
    if retail < 100:
        return ((Decimal("1"), retail), (Decimal("10"), q2(retail * Decimal("0.95"))),
                (Decimal("25"), q2(retail * Decimal("0.90"))))
    return ((Decimal("1"), retail), (Decimal("5"), q2(retail * Decimal("0.96"))),
            (Decimal("10"), q2(retail * Decimal("0.92"))))


def wholesale_breaks(retail):
    w = wholesale_of(retail)
    return ((Decimal("5"), w), (Decimal("50"), q2(w * Decimal("0.94"))))


def unit_price(retail, tier, qty):
    """The price-list price for ``qty`` (the deepest break at or under it)."""
    breaks = wholesale_breaks(retail) if tier == "wholesale" else retail_breaks(retail)
    price = breaks[0][1]
    for min_qty, value in breaks:
        if qty >= min_qty:
            price = value
    return price


class Clock:
    """``django.utils.timezone.now`` for the step being run: its moment, plus
    one microsecond per call, so rows written in one step keep their order."""

    def __init__(self, at):
        self.at, self.ticks = at, 0

    def __call__(self):
        self.ticks += 1
        return self.at + datetime.timedelta(microseconds=self.ticks)


# --- the plan (deterministic: constants and one seeded RNG, nothing from the database) --


def build_plan():
    """Quote, order and purchase-order chains, by key. Pure data."""
    rng = random.Random(RNG_SEED)
    skus = []  # (sku, retail, stock code)
    for spec in PRODUCTS:
        for sku, _name, retail, _value in variant_rows(spec):
            skus.append((sku, retail, spec["stock"]))
    sellable = [s for s in skus if s[2] != "out"]

    def lines(n_min=1, n_max=4):
        picked = rng.sample(sellable, rng.randint(n_min, n_max))
        out = []
        for sku, retail, _stock in picked:
            if retail >= 300:
                qty = rng.choice((1, 1, 2, 3))
            elif retail >= 60:
                qty = rng.choice((2, 4, 5, 6, 10))
            else:
                qty = rng.choice((5, 10, 12, 20, 25, 40))
            out.append((sku, qty))
        return out

    n_customers = len(CUSTOMERS)
    quotes = []
    # (outcome, days ago created): accepted ones become orders.
    quote_shapes = (
        ("accepted", 86), ("accepted", 79), ("lost", 74), ("accepted", 70), ("accepted", 63), ("expired", 58),
        ("accepted", 55), ("accepted", 49), ("sent", 44), ("lost", 40), ("accepted", 37), ("accepted", 31),
        ("accepted", 26), ("negotiating", 22), ("accepted", 19), ("sent", 16), ("accepted", 14),
        ("negotiating", 11), ("sent", 9), ("accepted", 8), ("sent", 6), ("draft", 4), ("draft", 3), ("draft", 2),
    )
    for i, (outcome, days) in enumerate(quote_shapes):
        quotes.append({
            "key": f"q{i + 1:02d}", "customer": (i * 7 + 3) % n_customers, "days": days, "outcome": outcome,
            "lines": lines(2, 5), "accept_after": rng.randint(2, 6), "shipping": rng.choice((0, 0, 45, 85, 120)),
            "flow": rng.choice(("normal", "normal", "normal", "late_payer", "partial_payer")),
        })
    orders = []
    order_shapes = (
        (89, "normal"), (84, "normal"), (81, "normal"), (77, "late_payer"), (72, "normal"), (68, "late_payer"),
        (65, "partial_payer"), (61, "normal"), (57, "normal"), (53, "cancel"), (50, "normal"), (46, "late_payer"),
        (43, "normal"), (39, "split"), (35, "late_payer"), (33, "partial_payer"), (29, "normal"), (27, "prepaid"),
        (24, "normal"), (21, "split"), (18, "normal"), (15, "hold"), (12, "normal"), (10, "backorder"),
        (7, "normal"), (5, "prepaid"), (4, "normal"), (3, "stall"), (2, "normal"), (1, "normal"),
    )
    channels = ("phone", "email", "storefront", "sales_rep", "email", "phone")
    for i, (days, flow) in enumerate(order_shapes):
        orders.append({
            "key": f"o{i + 1:02d}", "customer": (i * 11 + 1) % n_customers, "days": days, "flow": flow,
            "lines": lines(1, 4), "channel": channels[i % len(channels)],
            "shipping": rng.choice((0, 25, 45, 60, 95)),
        })
    pos = []
    po_shapes = (
        (85, "closed"), (76, "received"), (66, "received"), (58, "closed"), (47, "received"), (38, "partial"),
        (30, "received"), (22, "partial"), (14, "acknowledged"), (8, "sent"), (5, "sent"), (2, "draft"),
    )
    supplier_codes = [s[0] for s in SUPPLIERS]
    by_supplier = {}
    for spec in PRODUCTS:
        for sku, _n, retail, _v in variant_rows(spec):
            by_supplier.setdefault(BRAND_SUPPLIER[spec["brand"]], []).append((sku, retail, spec["stock"]))
    for i, (days, flow) in enumerate(po_shapes):
        code = supplier_codes[i % len(supplier_codes)]
        pool = by_supplier[code]
        if flow in ("sent", "acknowledged", "draft"):
            # Open orders replenish what is short (low, out or on backorder) first.
            short = [p for p in pool if p[2] != "ok"]
            picked = (short + rng.sample(pool, min(len(pool), 2)))[:3]
        else:
            picked = rng.sample([p for p in pool if p[2] == "ok"], min(3, len([p for p in pool if p[2] == "ok"])))
        seen, po_lines = set(), []
        for sku, retail, _stock in picked:
            if sku in seen:
                continue
            seen.add(sku)
            po_lines.append((sku, rng.choice((10, 20, 24, 30, 50)) if retail < 150 else rng.choice((4, 6, 8))))
        pos.append({"key": f"p{i + 1:02d}", "supplier": code, "days": days, "flow": flow, "lines": po_lines,
                    "warehouse": "WH-EAST" if i % 3 == 2 else "WH-MAIN",
                    "freight": rng.choice((0, 0, 65, 120))})
    return {"quotes": quotes, "orders": orders, "pos": pos}


# --- the command ------------------------------------------------------------------------


class Command(BaseCommand):
    help = ("Seed a demo company's catalog, customers, suppliers and 90 days of quotes, orders, invoices, "
            "payments and purchase orders; idempotent.")

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company pk (default: GOLDEN_PORTAL_COMPANY, or the only one).")
        parser.add_argument("--force", action="store_true",
                            help="Run although DEMO_USERNAMES is unset (never on a real company's database).")

    def handle(self, *args, **options):
        if not settings.DEMO_USERNAMES and not options.get("force"):
            raise CommandError("Refusing: DEMO_USERNAMES is not set, so this is not a demo deployment. "
                               "seed_demo_commerce writes fake customers, orders and books into the company; "
                               "pass --force only on a throwaway demo copy.")
        from core import demo_clock
        from core.plugins.dispatch import suppressed

        company = self._company(options.get("company"))
        self._check_prerequisites(company)
        self.zone = ZoneInfo(settings.TIME_ZONE)
        self.anchor = demo_clock.get_anchor() or demo_clock.local_today()
        self.company = company
        self.counts = {}
        clock = Clock(self.moment(120, "08:00"))
        with suppressed(), transaction.atomic(), mock.patch("django.utils.timezone.now", new=clock):
            self._catalog()
            self._parties()
            self._run_chains(clock)
        demo_clock.ensure_anchor(self.anchor, stdout=self.stdout)
        summary = ", ".join(f"{n} {what}" for what, n in self.counts.items() if n)
        # Demo setup scripts match "Commerce: seeded" to decide whether to
        # re-take the baseline snapshot: keep the words.
        if summary:
            self.stdout.write(self.style.SUCCESS(f"  Commerce: seeded {summary} in '{company.name}' "
                                                 f"(dated up to {self.anchor})."))
        else:
            self.stdout.write(f"  Commerce: '{company.name}' already has the demo catalog and documents; "
                              "nothing to add.")

    # -- setup ------------------------------------------------------------------------------

    def _company(self, pk):
        from core.models import Company

        wanted = pk or getattr(settings, "GOLDEN_PORTAL_COMPANY", None)
        if wanted:
            try:
                return Company.objects.get(pk=wanted)
            except (Company.DoesNotExist, ValueError) as exc:
                raise CommandError(f"No company {wanted!r}.") from exc
        companies = list(Company.objects.order_by("created_at")[:2])
        if len(companies) != 1:
            raise CommandError("Pass --company: there is not exactly one company.")
        return companies[0]

    def _check_prerequisites(self, company):
        from accounting.models import DocumentPostingRule
        from inventory.models import Warehouse
        from pricing.models.price_book import PriceList

        needed = {DocumentPostingRule.DocType.SALES_INVOICE, DocumentPostingRule.DocType.PAYMENT,
                  DocumentPostingRule.DocType.PURCHASE_RECEIPT}
        have = set(DocumentPostingRule.objects.filter(company=company).values_list("doc_type", flat=True))
        missing = []
        if needed - have:
            missing.append("posting rules for invoices, payments and receipts")
        if not Warehouse.objects.filter(company=company, warehouse_code="WH-MAIN").exists():
            missing.append("warehouse WH-MAIN")
        if not PriceList.objects.filter(name__in=["Standard Retail", "Wholesale Partner Tier 1"]).count() == 2:
            missing.append("the 'Standard Retail' and 'Wholesale Partner Tier 1' price lists")
        if missing:
            raise CommandError(f"'{company.name}' lacks {', '.join(missing)}: run seed_demo_data first.")

    def moment(self, days_ago, hhmm):
        """``hhmm`` local time, ``days_ago`` days before the anchor, as aware UTC."""
        hour, minute = map(int, hhmm.split(":"))
        day = self.anchor - datetime.timedelta(days=days_ago)
        local = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=self.zone)
        return local.astimezone(datetime.UTC)

    def day(self, days_ago):
        return self.anchor - datetime.timedelta(days=days_ago)

    def count(self, what, n=1):
        self.counts[what] = self.counts.get(what, 0) + n

    # -- catalog ----------------------------------------------------------------------------

    @staticmethod
    def _fill(obj, values):
        """Set the fields of ``values`` that are blank on ``obj``; save if any changed."""
        changed = [f for f, v in values.items() if v not in (None, "") and getattr(obj, f) in (None, "", {}, [])]
        for f in changed:
            setattr(obj, f, values[f])
        if changed:
            obj.save()
        return bool(changed)

    def _catalog(self):
        from inventory.models import Warehouse
        from pricing.models.price_book import PriceList, PriceListItem
        from products.models import (
            Brand,
            Category,
            Product,
            ProductCategory,
            ProductOption,
            ProductOptionValue,
            ProductVariant,
            SupplierProduct,
            VariantOptionValue,
        )
        from purchasing.models import Supplier

        company = self.company
        main = Warehouse.objects.get(warehouse_code="WH-MAIN")
        east, created = Warehouse.objects.get_or_create(
            warehouse_code="WH-EAST",
            defaults={"name": "East Coast Fulfillment", "address_line_1": "2400 Interstate Drive",
                      "city": "Harrisburg", "state_province": "PA", "postal_code": "17101", "country": "US",
                      "is_active": True, "company": company},
        )
        self.count("warehouses", int(created))
        self.warehouses = {"WH-MAIN": main, "WH-EAST": east}

        for code, name, display, kind, contact, city, state, terms, lead in SUPPLIERS:
            host = _domain(name)
            _, created = Supplier.objects.get_or_create(
                supplier_code=code,
                defaults={"company_name": name, "display_name": display, "supplier_type": kind,
                          "primary_contact_name": contact,
                          "primary_contact_email": f"{contact.split()[0].lower()}@{host}.example.com",
                          "po_email": f"orders@{host}.example.com", "website": f"https://{host}.example.com",
                          "city": city, "state_province": state, "country": "US", "payment_terms": terms,
                          "default_lead_time_days": lead, "is_active": True, "is_approved": True,
                          "company": company},
            )
            self.count("suppliers", int(created))
        self.suppliers = {s.supplier_code: s for s in Supplier.objects.filter(supplier_code__in=BRAND_SUPPLIER.values())}

        brands = {}
        for i, (key, (title, description, featured)) in enumerate(BRANDS.items()):
            brand, created = Brand.objects.get_or_create(title=title, defaults={"description": description})
            self.count("brands", int(created))
            host = slugify(title)
            self._fill(brand, {
                "description": description, "website_url": f"https://{host}.example.com",
                "seo_title": f"{title} | Industrial supply",
                "seo_meta_description": description[:155],
                "seo_og_title": title, "seo_og_description": description[:200],
                "logo_image_id": image_id("brands", f"{brand.slug}-logo.png"),
                "banner_image_id": image_id("brands", f"{brand.slug}-banner.jpg"),
            })
            if created:
                Brand.objects.filter(pk=brand.pk).update(is_featured_store=featured, sort_order=i,
                                                         visible_on_website=True)
            brands[key] = brand

        categories = {}
        for i, (key, (title, parent, short, featured)) in enumerate(CATEGORIES.items()):
            cat, created = Category.objects.get_or_create(
                title=title, defaults={"description": short, "parent": categories.get(parent),
                                       "sort_order": i, "is_featured_store": featured})
            self.count("categories", int(created))
            self._fill(cat, {
                "description": short, "short_description": short,
                "seo_title": f"{title} | Industrial supply",
                "seo_meta_description": f"{short} Volume pricing, stock in two warehouses and fast shipping."[:155],
                "seo_og_title": title, "seo_og_description": short,
                "primary_image_id": image_id("categories", f"{cat.slug}.jpg"),
            })
            if cat.parent_id is None and parent is not None and created:
                cat.parent = categories[parent]
                cat.save()
            categories[key] = cat

        retail_list = PriceList.objects.get(name="Standard Retail")
        wholesale_list = PriceList.objects.get(name="Wholesale Partner Tier 1")
        self.price_lists = {"retail": retail_list, "wholesale": wholesale_list}
        self.variants = {}
        self.retail = {}
        for i, spec in enumerate(PRODUCTS):
            brand, cat = brands[spec["brand"]], categories[spec["cat"]]
            description = f"{spec['summary']}\n\n{DESCRIPTION_TAIL[spec['cat']]}"
            rows = variant_rows(spec)
            product, created = Product.objects.get_or_create(
                title=spec["title"],
                defaults={"brand": brand, "part_number": spec["part"], "is_active": True,
                          "visible_on_website": True, "company": company},
            )
            self.count("products", int(created))
            spec_lines = "; ".join(f"{k}: {v}" for k, v in spec["specs"].items())
            self._fill(product, {
                "brand": brand, "part_number": spec["part"], "company": company,
                "short_description": spec["summary"], "description": description,
                "specifications": dict(spec["specs"]),
                "mpn": spec["part"],
                "seo_title": f"{spec['title']} | {brand.title}"[:255],
                "seo_meta_description": spec["summary"][:155],
                "seo_og_title": spec["title"], "seo_og_description": spec["summary"][:200],
                "seo_keywords": ", ".join([spec["title"].lower(), brand.title.lower(), cat.title.lower()]),
                "seo_use_cases": spec_lines,
                "primary_image_id": image_id("products", f"{product.slug}.jpg"),
            })
            if created:
                Product.objects.filter(pk=product.pk).update(
                    has_variants=len(rows) > 1, is_featured_store=spec["featured"],
                    lead_time_days=2 if spec["stock"] == "ok" else 14)
            ProductCategory.objects.get_or_create(product=product, category=cat, defaults={"is_primary": True})

            option = None
            if spec["option"]:
                option, _ = ProductOption.objects.get_or_create(product=product, name=spec["option"])
            for n, (sku, name, retail, value) in enumerate(rows):
                status = {"out": "out_of_stock", "back": "backorder"}.get(spec["stock"], "in_stock")
                variant, created = ProductVariant.objects.get_or_create(
                    sku=sku,
                    defaults={"product": product, "name": name, "retail_price": retail,
                              "wholesale_price": wholesale_of(retail), "msrp_price": spec["msrp"],
                              "sale_price": spec["sale"], "stock_status": status, "sort_order": n,
                              "mpn": sku, "is_active": True, "visible_on_website": True,
                              "seo_title": f"{spec['title']}, {value} | {brand.title}"[:255] if value else "",
                              "seo_meta_description": f"{spec['summary']} {spec['option']}: {value}."[:300]
                              if value else ""},
                )
                self.count("variants", int(created))
                self.variants[sku] = variant
                self.retail[sku] = Decimal(variant.retail_price or retail)
                if option is not None:
                    opt_value, _ = ProductOptionValue.objects.get_or_create(option=option, value=value,
                                                                            defaults={"sort_order": n})
                    VariantOptionValue.objects.get_or_create(variant=variant, option_value=opt_value)
                for min_qty, price in retail_breaks(self.retail[sku]):
                    PriceListItem.objects.get_or_create(price_list=retail_list, variant=variant, min_qty=min_qty,
                                                        defaults={"price": price, "is_active": True})
                for min_qty, price in wholesale_breaks(self.retail[sku]):
                    PriceListItem.objects.get_or_create(price_list=wholesale_list, variant=variant, min_qty=min_qty,
                                                        defaults={"price": price, "is_active": True})
                supplier = self.suppliers[BRAND_SUPPLIER[spec["brand"]]]
                SupplierProduct.objects.get_or_create(
                    variant=variant, supplier=supplier,
                    defaults={"supplier_sku": f"{supplier.supplier_code[-3:]}-{sku}", "cost": cost_of(retail),
                              "lead_time_days": supplier.default_lead_time_days, "is_primary": True},
                )
                self._stock(variant, spec["stock"], i + n, main, east)
        self.costs = {sku: cost_of(price) for sku, price in self.retail.items()}

    def _stock(self, variant, code, n, main, east):
        """Stock rows in both warehouses, written once (receipts move them later)."""
        from inventory.models import WarehouseStock

        cheap = self.retail[variant.sku] < 50
        reorder_main, reorder_east = (Decimal(40), Decimal(15)) if cheap else (Decimal(8), Decimal(4))
        if code in ("out", "back"):
            quantities = (0, 0)
        elif code == "low":
            quantities = (int(reorder_main) - 2 - n % 3, 0)
        else:
            base = 120 if cheap else 18
            quantities = (base + (n * 37) % (base * 2), base // 3 + (n * 23) % base)
        for warehouse, qty, reorder, bin_prefix in ((main, quantities[0], reorder_main, "A"),
                                                    (east, quantities[1], reorder_east, "E")):
            _, created = WarehouseStock.objects.get_or_create(
                warehouse=warehouse, variant=variant,
                defaults={"quantity": Decimal(qty), "reorder_level": reorder, "reorder_qty": reorder * 3,
                          "bin_location": f"{bin_prefix}-{1 + n % 9:02d}-{1 + (n * 7) % 20:02d}",
                          "valuation_rate": cost_of(self.retail[variant.sku])},
            )
            self.count("stock rows", int(created))

    # -- customers --------------------------------------------------------------------------

    def _parties(self):
        from core.models import UserProfile
        from customers.models import Customer, CustomerAddress, CustomerContact

        company = self.company
        self.reps = []
        for name in REPS:
            rep = UserProfile.objects.filter(company=company, user__isnull=True, display_name=name).first()
            self.reps.append(rep or UserProfile.objects.create(company=company, display_name=name))
        self.customers = []
        for i, (org, first, last, tier, terms, street, city, state, postal, area, exempt, rep) in enumerate(CUSTOMERS):
            host = _domain(org)
            email = f"{first.lower()}.{last.lower()}@{host}.example.com"
            phone = f"+1-{area}-555-01{i + 10:02d}"
            customer = Customer.objects.filter(company=company, email__iexact=email).first()
            if customer is None:
                customer = Customer.objects.create(
                    company=company, email=email, first_name=first, last_name=last, company_name=org,
                    customer_type=Customer.CustomerType.BUSINESS, pricing_tier=tier, phone=phone,
                    default_price_list=self.price_lists[tier], default_sales_rep=self.reps[rep],
                    is_tax_exempt=bool(exempt), tax_exempt_reason=exempt,
                    tax_exempt_certificate_number=f"EX-{state}-{40210 + i}" if exempt else "",
                    channel="sales_rep", lead_source="trade_show" if i % 3 == 0 else "referral",
                    notes=f"Payment terms: {terms}.",
                )
                self.count("customers")
                CustomerContact.objects.create(customer=customer, first_name=first, last_name=last, email=email,
                                               phone=phone, job_title="Purchasing manager", is_primary=True)
                for kind, label, is_default in ((CustomerAddress.AddressType.BILLING, "Accounts payable", True),
                                                (CustomerAddress.AddressType.SHIPPING, "Receiving dock", True)):
                    CustomerAddress.objects.create(
                        customer=customer, company=company, address_type=kind, is_default=is_default, label=label,
                        first_name=first, last_name=last, company_name=org, address_line_1=street, city=city,
                        state_province=state, postal_code=postal, country="US", phone=phone,
                        is_residential=False, has_loading_dock=kind == CustomerAddress.AddressType.SHIPPING,
                    )
            self.customers.append((customer, tier, terms))

    def _addresses(self, customer):
        from customers.models import CustomerAddress

        rows = {a.address_type: a for a in customer.addresses.order_by("-is_default", "created_at")}
        return rows.get(CustomerAddress.AddressType.BILLING), rows.get(CustomerAddress.AddressType.SHIPPING)

    # -- chains -----------------------------------------------------------------------------

    def _run_chains(self, clock):
        """Run every chain's steps in date order (a heap of generators)."""
        from orders.models import Order, Quote
        from purchasing.models import PurchaseOrder

        plan = build_plan()
        done_quotes = set(Quote._base_manager.filter(company=self.company, metadata__seed=SEED_TAG)
                          .values_list("metadata__seed_index", flat=True))
        done_orders = set(Order._base_manager.filter(company=self.company, metadata__seed=SEED_TAG)
                          .values_list("metadata__seed_index", flat=True))
        done_pos = set(PurchaseOrder._base_manager.filter(company=self.company, metadata__seed=SEED_TAG)
                       .values_list("metadata__seed_index", flat=True))
        chains = (
            [self._quote_chain(s) for s in plan["quotes"] if s["key"] not in done_quotes]
            + [self._order_chain(s) for s in plan["orders"] if s["key"] not in done_orders]
            + [self._po_chain(s) for s in plan["pos"] if s["key"] not in done_pos]
        )
        heap = []
        for seq, gen in enumerate(chains):
            first = next(gen, None)
            if first is not None:
                heapq.heappush(heap, (first, seq, gen))
        latest = self.moment(1, "23:59")  # nothing is dated after yesterday
        while heap:
            at, seq, gen = heapq.heappop(heap)
            clock.at = at
            nxt = next(gen, None)  # run this step; get the chain's next moment
            if nxt is None:
                continue
            if nxt > latest:
                gen.close()  # the rest of this chain lies in the future: it has not happened yet
                continue
            if nxt < at:
                raise CommandError(f"Chain step out of order: {nxt} after {at}.")
            heapq.heappush(heap, (nxt, seq, gen))

    # A chain is a generator: it yields the moment of its next step, and the
    # code after that ``yield`` is the step (run with the clock at that moment).

    def _later(self, at, **delta):
        return at + datetime.timedelta(**delta)

    def _local_day(self, at=None):
        from django.utils import timezone

        return (at or timezone.now()).astimezone(self.zone).date()

    def _line_values(self, sku, qty, tier):
        variant = self.variants[sku]
        qty = Decimal(qty)
        price = unit_price(self.retail[sku], tier, qty)
        cost = self.costs[sku]
        product = variant.product
        name = product.title if variant.name == product.title else f"{product.title} ({variant.name})"
        total = q2(price * qty)
        return {
            "variant": variant, "item_name": name[:255], "sku": sku, "quantity": qty,
            "unit_price_ex_tax": price, "unit_price_inc_tax": price, "list_price": self.retail[sku],
            "line_total_ex_tax": total, "line_total_inc_tax": total, "price_list": self.price_lists[tier],
            "unit_cost": cost, "margin_amount": q2((price - cost) * qty),
            "margin_percent": q2((price - cost) / price * 100) if price else ZERO,
        }

    def _quote_chain(self, s):
        from crm import engagements
        from orders.convert import convert_quote_to_order
        from orders.models import Quote, QuoteLineItem, ShipmentGroup

        customer, tier, terms = self.customers[s["customer"]]
        t0 = self.moment(s["days"], "09:30")
        yield t0
        billing, shipping = self._addresses(customer)
        quote = Quote.objects.create(
            company=self.company, customer=customer, billing_address=billing, shipping_address=shipping,
            contact=customer.contacts.order_by("-is_primary", "created_at").first(),
            sales_rep=customer.default_sales_rep, price_list=self.price_lists[tier], channel="sales_rep",
            validity_period_days=30, quote_expiration_date=self.day(s["days"] - 30),
            payment_terms_proposed=terms, shipping_terms_proposed="Prepaid & Add",
            shipping_cost_ex_tax=Decimal(s["shipping"]), customer_notes="Prices valid for 30 days.",
            metadata={"seed": SEED_TAG, "seed_index": s["key"]},
        )
        group = ShipmentGroup.objects.create(quote=quote, name="Shipment 1", warehouse=self.warehouses["WH-MAIN"],
                                             fulfillment_type=ShipmentGroup.FulfillmentType.INTERNAL)
        for n, (sku, qty) in enumerate(s["lines"]):
            QuoteLineItem.objects.create(quote=quote, shipment_group=group, sort_order=n,
                                         **self._line_values(sku, qty, tier))
        self.count("quotes")
        quote.refresh_from_db()
        outcome = s["outcome"]
        if outcome == "draft":
            return
        eng = quote.engagement
        stages = {st.key: st for st in eng.pipeline.stages.filter(is_active=True)}

        yield self._later(t0, hours=3)
        quote.status = Quote.Status.SENT
        quote.sent_at, quote.sent_via, quote.sent_to_email = self._now(), "email", customer.email
        quote.save(update_fields=["status", "sent_at", "sent_via", "sent_to_email", "updated_at"])
        if "proposal_sent" in stages and eng.stage_id != stages["proposal_sent"].pk:
            engagements.move(eng, stages["proposal_sent"], source=IMPORT_SOURCE)
        if outcome == "sent":
            return

        if outcome in ("negotiating", "accepted"):
            yield self._later(t0, days=1, hours=5)
            quote.status = Quote.Status.NEGOTIATING
            quote.save(update_fields=["status", "updated_at"])
            if "negotiation" in stages:
                engagements.move(eng, stages["negotiation"], source=IMPORT_SOURCE,
                                 note="Customer asked about volume pricing")
            if outcome == "negotiating":
                return
            yield self.moment(s["days"] - s["accept_after"], "09:15")
            quote.refresh_from_db()
            quote.customer_po_number = f"{_domain(customer.company_name)[:6].upper()}-{4100 + s['days']}"
            quote.acceptance_method = Quote.AcceptanceMethod.PO_RECEIVED
            quote.save(update_fields=["customer_po_number", "acceptance_method", "updated_at"])
            order, _ = convert_quote_to_order(quote)
            self.count("quotes converted")
            yield from self._fulfil(order, s, s["days"] - s["accept_after"], terms, tier)
        elif outcome == "lost":
            yield self._later(t0, days=6)
            quote.status = Quote.Status.LOST
            quote.status_note = "Went with a regional distributor on price."
            quote.save(update_fields=["status", "status_note", "updated_at"])
            engagements.lose(eng, "price", competitor="A regional distributor", source=IMPORT_SOURCE)
        elif outcome == "expired":
            yield self.moment(s["days"] - 31, "08:00")
            quote.status = Quote.Status.EXPIRED
            quote.save(update_fields=["status", "updated_at"])
            engagements.lose(eng, "expired", source=IMPORT_SOURCE, note="No reply before the quote expired")

    @staticmethod
    def _now():
        from django.utils import timezone

        return timezone.now()

    def _order_chain(self, s):
        from customers.models import Customer
        from orders.models import Order, OrderLineItem, ShipmentGroup

        customer, tier, terms = self.customers[s["customer"]]
        if s["flow"] == "prepaid":
            terms = "Prepaid"
        t0 = self.moment(s["days"], "10:15")
        yield t0
        billing, shipping = self._addresses(customer)
        order = Order.objects.create(
            company=self.company, customer=customer, billing_address=billing, shipping_address=shipping,
            contact=customer.contacts.order_by("-is_primary", "created_at").first(),
            sales_rep=customer.default_sales_rep, price_list=self.price_lists[tier], channel=s["channel"],
            payment_terms=terms, order_date=self._now(), status=Order.Status.PENDING,
            shipping_cost_ex_tax=Decimal(s["shipping"]), shipping_cost_inc_tax=Decimal(s["shipping"]),
            customer_po_number=f"{_domain(customer.company_name)[:6].upper()}-{7300 + s['days']}",
            tax_exempt=customer.is_tax_exempt, metadata={"seed": SEED_TAG, "seed_index": s["key"]},
        )
        split = s["flow"] == "split" and len(s["lines"]) > 1
        groups = [ShipmentGroup.objects.create(order=order, name="Shipment 1", warehouse=self.warehouses["WH-MAIN"],
                                               fulfillment_type=ShipmentGroup.FulfillmentType.INTERNAL)]
        if split:
            groups.append(ShipmentGroup.objects.create(
                order=order, name="Shipment 2", warehouse=self.warehouses["WH-EAST"], sort_order=1,
                fulfillment_type=ShipmentGroup.FulfillmentType.INTERNAL))
        for n, (sku, qty) in enumerate(s["lines"]):
            OrderLineItem.objects.create(order=order, shipment_group=groups[n % len(groups)], sort_order=n,
                                         **self._line_values(sku, qty, tier))
        Customer._base_manager.filter(pk=customer.pk).update(last_order_at=self._now())
        self.count("orders")
        yield from self._fulfil(order, s, s["days"], terms, tier)

    def _fulfil(self, order, s, placed, terms, tier):
        """An order's life from PENDING: confirm, pick, ship, invoice, get paid, complete.
        Steps that would fall after yesterday never happen (the scheduler stops the chain)."""
        from orders.models import Order

        flow = s["flow"]
        if flow == "stall":
            return  # still pending: entered, not confirmed yet
        if flow == "cancel":
            yield self.moment(placed - 1, "09:10")
            order.refresh_from_db()
            order.status_note = "Customer canceled: project postponed."
            order.save(update_fields=["status_note", "updated_at"])
            order.transition_to(Order.Status.CANCELLED)
            return
        prepaid = terms.lower() == "prepaid"
        if prepaid:
            yield self.moment(placed, "10:45")
            self._confirm(order, Order.Status.AWAITING_PAYMENT)
            invoice = self._invoice(order)
            yield self.moment(placed, "14:20")
            # Settling the card payment moves the order on to awaiting fulfillment.
            self._pay(order, invoice, invoice.balance_due, "credit_card", s["key"], 1)
        else:
            yield self.moment(placed, "11:00")
            self._confirm(order, Order.Status.AWAITING_FULFILLMENT)
            invoice = None
        if flow == "hold":
            yield self.moment(placed - 1, "10:00")
            order.refresh_from_db()
            order.status_note = "On hold: credit limit review."
            order.save(update_fields=["status_note", "updated_at"])
            order.transition_to(Order.Status.ON_HOLD)
            return
        if flow == "backorder":
            yield self.moment(placed - 1, "10:00")
            order.refresh_from_db()
            order.status_note = "Backordered: waiting on the supplier's next delivery."
            order.save(update_fields=["status_note", "updated_at"])
            order.transition_to(Order.Status.BACKORDERED)
            return

        groups = list(order.shipment_groups.order_by("sort_order", "pk"))
        ticketed = placed <= 25
        yield self.moment(placed - 1, "09:30")
        order.refresh_from_db()
        order.transition_to(Order.Status.AWAITING_SHIPMENT)
        tickets = [self._pick_ticket(order, g) for g in groups] if ticketed else [None] * len(groups)

        # Shipments: the first two days after placing; a split order's second one much later.
        ship_days = [placed - 2] + [placed - 25] * (len(groups) - 1)
        events = []
        for group, ticket, day in zip(groups, tickets, ship_days, strict=True):
            events.append((self.moment(day, "15:30"), "ship", (group, ticket)))
        final_ship = ship_days[-1]
        events.append((self.moment(final_ship, "16:30"), "invoice", None))
        net = {"net 15": 15, "net 30": 30, "net 45": 45, "net 60": 60}.get(terms.lower(), 0)
        # Payment arrives inside the terms (due on receipt: within a week); a
        # late payer never pays, and its invoice turns overdue a day after the
        # due date (due on receipt: ten days of grace). An invoice whose
        # payment would fall after yesterday is paid yesterday for some keys,
        # so "this month" always has receipts in it after the first.
        key_n = sum(map(ord, s["key"]))
        if flow != "late_payer" and not prepaid:
            lag = max(3, int(net * 0.7)) + key_n % 5
            pay_at = self.moment(final_ship - lag, "13:40")
            if pay_at > self.moment(1, "23:59") and key_n % 3 == 0 and final_ship >= 3:
                pay_at = self.moment(1, "10:30")
            share = Decimal("0.4") if flow == "partial_payer" else Decimal("1")
            events.append((pay_at, "pay", share))
            # An order is complete once it has shipped and been paid in full.
            events.append((pay_at + datetime.timedelta(hours=2), "complete", None))
        events.append((self.moment(final_ship - max(net, 10) - 1, "07:30"), "overdue", None))
        events.append((self.moment(final_ship - 6, "12:00"), "complete", None))
        events.sort(key=lambda e: e[0])
        method = "credit_card" if tier == "retail" else ("ach" if s["days"] % 2 else "check")
        for at, kind, arg in events:
            yield at
            order.refresh_from_db()
            if kind == "ship":
                self._ship(order, *arg, last=arg[0].pk == groups[-1].pk)
            elif kind == "invoice":
                invoice = invoice or self._invoice(order)
            elif kind == "pay" and invoice is not None:
                invoice.refresh_from_db()
                amount = q2(Decimal(invoice.balance_due) * arg)
                if amount > 0:
                    self._pay(order, invoice, amount, method, s["key"], 2)
            elif kind == "overdue" and invoice is not None:
                invoice.refresh_from_db()
                if invoice.status == invoice.Status.SENT:
                    invoice.status = invoice.Status.OVERDUE
                    invoice.save(update_fields=["status", "updated_at"])
                    self.count("invoices overdue")
            elif (kind == "complete" and order.status == Order.Status.SHIPPED
                  and order.payment_status == Order.PaymentStatus.PAID):
                order.transition_to(Order.Status.COMPLETED)

    def _confirm(self, order, status):
        order.refresh_from_db()
        order.booked_at = self._now()
        order.save(update_fields=["booked_at", "updated_at"])
        order.transition_to(status)

    def _pick_ticket(self, order, group):
        """A pick ticket for one shipment group, built as the back office's
        "Create pick ticket" button builds it (fulfillment.erp_views)."""
        from fulfillment.models import PickTicket, PickTicketLine
        from inventory.models import WarehouseStock

        address = order.shipping_address
        lines = list(order.line_items.filter(shipment_group=group).select_related("variant"))
        bins = dict(WarehouseStock.objects.filter(warehouse=group.warehouse,
                                                  variant_id__in=[li.variant_id for li in lines])
                    .values_list("variant_id", "bin_location"))
        ticket = PickTicket.objects.create(
            order=order, shipment_group=group, warehouse=group.warehouse,
            ship_to_label=address.label if address else "",
            ship_to_attention=" ".join(filter(None, [address.first_name, address.last_name])) if address else "",
            ship_to_address="\n".join(filter(None, [
                address.company_name, address.address_line_1, address.address_line_2,
                f"{address.city}, {address.state_province} {address.postal_code}"])) if address else "",
            metadata={"seed": SEED_TAG},
        )
        PickTicketLine.objects.bulk_create([
            PickTicketLine(pick_ticket=ticket, order_line_item=li, variant=li.variant, item_name=li.item_name,
                           sku=li.sku, quantity=li.quantity, unit_cost=li.unit_cost or 0,
                           line_total=q2(Decimal(li.unit_cost or 0) * li.quantity),
                           bin_location=bins.get(li.variant_id, ""), sort_order=i)
            for i, li in enumerate(lines)
        ])
        ticket.assigned_to = self.reps[0]
        ticket.assigned_at = self._now()
        ticket.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
        for status in ("assigned", "picking"):
            ticket.transition_to(status)
        self.count("pick tickets")
        return ticket

    def _ship(self, order, group, ticket, *, last):
        from django.db.models import F

        from orders.models import Order, OrderLineItem

        now = self._now()
        if ticket is not None:
            ticket.lines.update(picked_qty=F("quantity"), status="picked")
            ticket.transition_to("picked")
            ticket.picked_at = now
            for status in ("packing", "packed", "shipped"):
                ticket.transition_to(status)
            ticket.packed_at = ticket.shipped_at = now
            ticket.save(update_fields=["picked_at", "packed_at", "shipped_at", "updated_at"])
        OrderLineItem.objects.filter(order=order, shipment_group=group).update(
            quantity_shipped=F("quantity"), fulfillment_status=OrderLineItem.FulfillmentStatus.SHIPPED,
            updated_at=now)
        order.shipped_date = now
        order.fulfillment_status = (Order.FulfillmentStatus.FULFILLED if last
                                    else Order.FulfillmentStatus.PARTIAL)
        order.save(update_fields=["shipped_date", "fulfillment_status", "updated_at"])
        order.transition_to(Order.Status.SHIPPED if last else Order.Status.PARTIALLY_SHIPPED)

    def _invoice(self, order):
        """Invoice the order (``orders.convert``), dated today on the demo clock,
        and send it: leaving draft posts it under the invoice posting rule."""
        from orders.convert import _due_date_for, convert_order_to_invoice
        from orders.models import SalesInvoice

        invoice, _ = convert_order_to_invoice(order)
        # convert reads date.today(), which is the real day: the demo's is the clock's.
        today = self._local_day()
        SalesInvoice.objects.filter(pk=invoice.pk).update(
            invoice_date=today, due_date=_due_date_for(today, invoice.payment_terms) or today)
        invoice.refresh_from_db()
        invoice.status = SalesInvoice.Status.SENT
        invoice.sent_at = self._now()
        invoice.save()
        self.count("invoices")
        return invoice

    def _pay(self, order, invoice, amount, method, key, n):
        from orders.models import SalesInvoice
        from payments.services import record_payment

        record_payment(invoice=invoice, amount=amount, payment_date=self._now(), payment_method=method,
                       reference=f"{method.upper()}-{invoice.invoice_number}-{n}",
                       notes="Recorded by the demo seed.", idempotency_key=f"{SEED_TAG}:{key}:{n}")
        # Settlement stamps the paid date from date.today(), the real day.
        SalesInvoice.objects.filter(pk=invoice.pk, paid_date__isnull=False).update(paid_date=self._local_day())
        invoice.refresh_from_db()
        self.count("payments")

    def _po_chain(self, s):
        from purchasing.models import POLineItem, PurchaseOrder
        from purchasing.services import receive_purchase_order

        supplier = self.suppliers[s["supplier"]]
        lead = supplier.default_lead_time_days or 5
        t0 = self.moment(s["days"], "08:45")
        yield t0
        warehouse = self.warehouses[s["warehouse"]]
        po = PurchaseOrder.objects.create(
            company=self.company, supplier=supplier, warehouse=warehouse,
            po_type=PurchaseOrder.POType.STOCK_REPLENISHMENT,
            supplier_contact_name=supplier.primary_contact_name, supplier_contact_email=supplier.po_email,
            vendor_payment_terms=supplier.payment_terms, buyer=self.reps[1], requested_by=self.reps[1],
            requested_delivery_date=self.day(s["days"] - lead), expected_delivery_date=self.day(s["days"] - lead),
            ship_to_label=warehouse.name, shipping_cost=Decimal(s["freight"]),
            metadata={"seed": SEED_TAG, "seed_index": s["key"]},
        )
        for n, (sku, qty) in enumerate(s["lines"]):
            variant = self.variants[sku]
            sp = variant.supplier_products.filter(supplier=supplier).first()
            POLineItem.objects.create(
                purchase_order=po, sort_order=n, variant=variant, supplier_product=sp,
                item_name=variant.product.title if variant.name == variant.product.title
                else f"{variant.product.title} ({variant.name})",
                supplier_sku=sp.supplier_sku if sp else "", our_sku=sku, quantity_ordered=Decimal(qty),
                unit_cost=self.costs[sku], list_cost=self.costs[sku], line_total=q2(self.costs[sku] * qty),
            )
        self.count("purchase orders")
        flow = s["flow"]
        if flow == "draft":
            return
        yield self._later(t0, hours=2)
        po.refresh_from_db()
        po.sent_at, po.sent_via = self._now(), "email"
        po.save(update_fields=["sent_at", "sent_via", "updated_at"])
        po.transition_to(PurchaseOrder.Status.SENT)
        if flow == "sent":
            return
        yield self.moment(s["days"] - 1, "10:20")
        po.refresh_from_db()
        po.acknowledged_at = self._now()
        po.vendor_order_number = f"{supplier.supplier_code[-3:]}-SO-{58000 + s['days'] * 7}"
        po.save(update_fields=["acknowledged_at", "vendor_order_number", "updated_at"])
        po.transition_to(PurchaseOrder.Status.ACKNOWLEDGED)
        if flow == "acknowledged":
            return
        yield self.moment(s["days"] - lead + 2, "15:00")
        po.refresh_from_db()
        po.actual_ship_date = self._now()
        po.carrier, po.tracking_number = "Freight line", f"PRO{s['days']:03d}{len(s['key']) * 4417}"
        po.save(update_fields=["actual_ship_date", "carrier", "tracking_number", "updated_at"])
        po.transition_to(PurchaseOrder.Status.SHIPPED)
        yield self.moment(s["days"] - lead, "11:10")
        lines = list(po.line_items.order_by("sort_order"))
        if flow == "partial":
            wanted = {line: (line.quantity_ordered / 2).to_integral_value() or 1 for line in lines[:2]}
        else:
            wanted = {line: line.quantity_ordered for line in lines}
        receive_purchase_order(po, wanted, warehouse=warehouse, received_by=self.reps[0],
                               carrier=po.carrier, tracking_number=po.tracking_number,
                               notes="Received in good condition.", idempotency_key=f"{SEED_TAG}:{s['key']}:1")
        self.count("receipts")
        po.refresh_from_db()
        po.actual_delivery_date = self._now()
        po.save(update_fields=["actual_delivery_date", "updated_at"])
        if flow == "closed":
            yield self.moment(s["days"] - lead - 1, "09:00")
            po.refresh_from_db()
            po.transition_to(PurchaseOrder.Status.CLOSED)
