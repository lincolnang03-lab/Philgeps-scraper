"""Rule-based (no AI) categorisation of PhilGEPS bid notices.

PhilGEPS tags each notice with one of ~168 fine-grained "business categories".
That is too granular to be useful for browsing, and some notices are filed
under vague or wrong categories (e.g. vaccines under "Chemicals and Chemical
Products"). We therefore assign each notice to one of a small set of SECTORS:

1. If the site category is specific, map it straight to its sector.
2. If the site category is vague (see AMBIGUOUS_SITE_CATEGORIES) or missing,
   match keywords in the title/description; fall back to the mapped sector.
3. Otherwise -> "Uncategorized".

Extra deterministic tags are also derived: region (from Area of Delivery)
and budget bracket (from the Approved Budget for the Contract).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Sector -> PhilGEPS site categories (exact strings as shown on the site)
# --------------------------------------------------------------------------
SECTORS: Dict[str, List[str]] = {
    "Construction & Infrastructure": [
        "Construction Projects", "General Contractor", "Construction Management Services",
        "Construction Materials and Supplies", "Hardware and Construction Supplies",
        "Construction Equipment", "Metal Fabrication", "Horizontal Directional Drilling",
        "Well Drilling and Construction Services", "Real Estate Developement and Maintenance",
    ],
    "Health & Medical": [
        "Drugs and Medicines", "Medical Supplies and Laboratory Instrument",
        "Hospital / Medical Equipment", "Hospital / Medical Equipment Services",
        "Medical and Dental Equipment", "Radiological/Diagnostic Equipment",
        "Diagnostic and Laboratory Services", "Laboratory Supplies and Equipment",
    ],
    "IT & Telecommunications": [
        "Information Technology", "Information Technology Parts & Accessories & Perip",
        "Internet Services", "IT Broadcasting and Telecommunications", "Systems Integration",
        "Structured Cabling", "Telecommunications Engineering", "Telecommunications Provider",
        "Communication Equipment", "Communication Equipment & Parts and Accessories",
        "Electronic Parts and Components",
    ],
    "Food & Catering": [
        "Food Stuff", "Beverages", "Dairy Products", "Grocery Items", "Packed Food Distributor",
        "Preserved or Processed Foods", "Catering Services", "Institutional food services equipment",
        "Food Processing Equipment", "Kitchenware",
    ],
    "Agriculture & Fisheries": [
        "Agricultural Chemicals", "Agricultural Machinery and Equipment",
        "Agricultural Products (Seeds, Seedlings, Plants..)", "Animal Feeds", "Fertilizers",
        "Live Animals (Livestock, Birds, Live fish & etc..)", "Veterinary Products and Supplies",
        "Rice Milling Services", "Pest Control Products",
    ],
    "Vehicles, Fuel & Transport Equipment": [
        "Vehicles", "Vehicle Parts and Accessories", "Vehicle Repair and Maintenance",
        "Aircraft Spare Parts", "Aviation Products", "Helicopters - Parts", "Marine Transport",
        "Maritime Spare Parts", "Navigation Equipment",
        "Fuels/Fuel Additives & Lubricants & Anti Corrosive",
    ],
    "Logistics & Transport Services": [
        "Cargo Forwarding and Hauling Services", "Freight Forwarder Services",
        "Mail and Cargo Transport Services", "Transportation and Communications Services",
        "Aviation Services",
    ],
    "Office Supplies & Equipment": [
        "Office Equipment", "Office Equipment Parts and Accessories",
        "Office Equipment Supplies and Consumables", "Office Supplies and Devices",
        "Paper Materials and Products", "Printing Supplies", "Mailing Supplies",
        "Microfilm Equipment", "Microfilm Equipment - Supplies and Accesories",
        "Packaging Supplies and Materials",
    ],
    "Furniture, Fixtures & Appliances": [
        "Furniture", "Furniture Parts and Accessories", "Computer Furniture", "Fixtures",
        "Appliances", "Airconditioning and Airconditioning Systems", "Audio and Visual Equipment",
    ],
    "Power, Water & Utilities": [
        "Electrical Supplies", "Electrical Systems and Lighting Components",
        "Power Generation and Distribution Machinery", "Transmission and Distribution Lines",
        "Sub-station Contractors", "Water and Waste Water Treatment Supply & Disposal",
        "Water Service Connection Materials/Fittings", "Hydrological Instruments",
        "Meteorological Equipments and Instruments", "Traffic Control Systems",
    ],
    "Industrial & Scientific Equipment": [
        "Industrial Machinery and Equipment", "Industrial pumps and compressors", "Machine Tools",
        "Lifting equipment and accessories", "Mining Equipment and Supplies", "Automation Equipment",
        "Engineering and Laboratory Testing Equipment", "Geotechnical Instrumentation",
        "Surveying Instruments", "Chemicals and Chemical Products",
        "Oil/Heat Chemical Resistant Rubber",
    ],
    "Security, Defense & Safety": [
        "Security Services", "Security Surveillance and Detection Equipment", "Guns and Weapons",
        "Ammunitions and Explosives", "Ordnance Products", "Investigative Equipment",
        "Fire Fighting & Rescue and Safety Equipment", "Industrial Safety Equipment",
        "Safety and Occupational Products", "Environmental Health/Safety Equipment",
        "Quartermaster Items",
    ],
    "Facilities & Maintenance Services": [
        "Janitorial Services", "Janitorial Supplies", "Janitorial Equipment", "Chemical Detergents",
        "Laundry Services", "Landscaping Services", "Pest Control Services",
        "General Repair and Maintenance Services", "Airconditioning Maintenance Services",
        "Waste Management and Recycling", "Lease and Rental of Property or Building",
        "Sale of Property or Building",
    ],
    "Professional & Consulting Services": [
        "Consulting Services", "Architectural Design", "General Engineering Services",
        "Surveying Services", "Market Research Services", "Financial and Insurance Services",
        "Services",
    ],
    "Media, Printing & Events": [
        "Advertising Agency Services", "Print and Broadcast and Aerial Advertising",
        "Public Relations Programs or Services", "Printing Services", "Reproduction Services",
        "Editorial, Design, Graphic and Fine Art Services", "Video Production Services",
        "Photography Services", "Photographic Equipment",
        "Photographic Parts, Supplies and Accessories", "Newspapers", "Events Management",
        "Hotel and Lodging and Meeting Facilities", "Travel, Food, Lodging and Entertainment Services",
        "Signage and Accessories", "Flags", "Tokens and Awards", "Corporate Giveaways",
        "Pyrotechnics and Fireworks",
    ],
    "Education, Sports & Culture": [
        "Educational Materials and Supplies", "Books, Maps and Other Publications",
        "Education and Training Services", "Sporting Goods", "Games and Toys",
        "Musical Instruments", "Musical Instrument Parts and Accesories",
        "Arts and Crafts Accessories and Supplies",
    ],
    "Clothing, Textiles & Personal Care": [
        "Garments", "Textiles", "Purses, handbags and bags", "Personal Care Products",
        "Bedclothes, Linens and Towels", "Timepieces and Jewelry and Gemstone Products",
    ],
    "General Merchandise & Other": [
        "General Merchandise", "Gaming Equipment and Paraphernalia",
        "Funeral and Associated Services", "Plastic Products",
    ],
}

UNCATEGORIZED = "Uncategorized"

# Site categories that are too vague (or often misused) to trust on their own.
# For these, title keywords win if any match.
AMBIGUOUS_SITE_CATEGORIES = {
    "General Merchandise", "Services", "General Repair and Maintenance Services",
    "Chemicals and Chemical Products", "General Contractor", "Plastic Products",
}

# Descriptions carry long legal boilerplate; only their opening is useful.
DESCRIPTION_CHARS = 300

# Ordered keyword rules: first sector with a match wins. Word-boundary regexes,
# case-insensitive, matched against "title + description".
KEYWORD_RULES: List[Tuple[str, str]] = [
    ("Health & Medical", r"vaccine|medicine|medicines|drugs?|pharma\w*|medical|hospital|"
                         r"dental|laborator(y|ies) reagents?|reagents?|syringe|"
                         r"(parenteral|parental) solutions?|iv fluids?|rhu|clinic"),
    ("Construction & Infrastructure", r"construction of|rehabilitation of|concreting|"
                         r"road|bridge|drainage|seawall|building|multi-?purpose hall|"
                         r"school ?building|flood control|asphalt|civil works|"
                         r"improvement of|riprap|slope protection"),
    ("Power, Water & Utilities", r"solar|street ?lights?|electrification|generator set|"
                         r"genset|water system|potable water|waterworks|transformer"),
    ("IT & Telecommunications", r"laptop|desktop|computer|printer|software|server|"
                         r"network|internet|ict|cctv|wifi|wi-fi|tablet|it equipment"),
    ("Food & Catering", r"meals?|snacks?|catering|food packs?|food supplies|food|rice|"
                         r"groceries|foodstuff|feeding|coffee|bread"),
    ("Vehicles, Fuel & Transport Equipment", r"vehicle|truck|tanker|ambulance|"
                         r"motorcycle|tires?|tyres?|batter(y|ies) for|fuel|gasoline|"
                         r"diesel|lubricants?|excavator|backhoe|dump truck"),
    ("Agriculture & Fisheries", r"seeds?|seedlings?|fertili[sz]ers?|livestock|"
                         r"piglets?|chicks?|fingerlings|fishing|farm|agricultur\w*|"
                         r"veterinary|carabao|cattle"),
    ("Office Supplies & Equipment", r"office supplies|bond paper|ink|toner|"
                         r"stationer(y|ies)|photocopier|copier( machine)?"),
    ("Security, Defense & Safety", r"security (guard|services)|firearms?|ammunition|"
                         r"fire ?extinguisher|fire truck|rescue|disaster|drrm"),
    ("Facilities & Maintenance Services", r"janitorial|cleaning|garbage|waste|"
                         r"aircon(ditioning)? (repair|maintenance)|pest control|"
                         r"landscaping|rental of|lease of"),
    ("Education, Sports & Culture", r"school supplies|instructional materials|"
                         r"textbooks?|learning materials|sports?|athletic|"
                         r"training of|seminar|workshop"),
    ("Clothing, Textiles & Personal Care", r"uniforms?|t-?shirts?|garments?|"
                         r"textiles?|hygiene kits?|toiletries"),
    ("Media, Printing & Events", r"printing of|tarpaulin|streamers?|souvenir|"
                         r"tokens?|venue|accommodation|events?|celebration|"
                         r"festival|sound system|lights and sounds"),
    ("Professional & Consulting Services", r"consult\w*|feasibility study|"
                         r"detailed engineering|design services|audit services|"
                         r"insurance"),
]

# Whole-word match, with an optional plural "s" on every keyword.
_COMPILED = [(s, re.compile(r"\b(?:%s)s?\b" % p, re.I)) for s, p in KEYWORD_RULES]
_SITE_TO_SECTOR: Dict[str, str] = {
    cat.lower(): sector for sector, cats in SECTORS.items() for cat in cats
}


def sector_for_site_category(site_category: Optional[str]) -> Optional[str]:
    if not site_category:
        return None
    return _SITE_TO_SECTOR.get(site_category.strip().lower())


_NEGATIONS = re.compile(r"\bnon-?\s?food\b", re.I)


def sector_from_keywords(text: str) -> Optional[str]:
    text = _NEGATIONS.sub(" ", text or "")
    for sector, rx in _COMPILED:
        if rx.search(text):
            return sector
    return None


def categorize(site_category: Optional[str], title: str = "",
               description: str = "") -> Tuple[str, str]:
    """Return (sector, source) where source is one of
    'site_category', 'title_keywords', 'uncategorized'."""
    mapped = sector_for_site_category(site_category)
    vague = (not site_category) or site_category.strip() in AMBIGUOUS_SITE_CATEGORIES
    if mapped and not vague:
        return mapped, "site_category"
    kw = (sector_from_keywords(title or "")
          or sector_from_keywords((description or "")[:DESCRIPTION_CHARS]))
    if kw:
        return kw, "title_keywords"
    if mapped:
        return mapped, "site_category"
    return UNCATEGORIZED, "uncategorized"


# --------------------------------------------------------------------------
# Region (from PhilGEPS "Area of Delivery", which is a province name)
# --------------------------------------------------------------------------
REGIONS: Dict[str, List[str]] = {
    "NCR": ["Metro Manila"],
    "CAR": ["Abra", "Apayao", "Benguet", "Ifugao", "Kalinga", "Mountain Province"],
    "Region I - Ilocos": ["Ilocos Norte", "Ilocos Sur", "La Union", "Pangasinan"],
    "Region II - Cagayan Valley": ["Batanes", "Cagayan", "Isabela", "Nueva Vizcaya", "Quirino"],
    "Region III - Central Luzon": ["Aurora", "Bataan", "Bulacan", "Nueva Ecija", "Pampanga",
                                   "Tarlac", "Zambales"],
    "Region IV-A - CALABARZON": ["Batangas", "Cavite", "Laguna", "Quezon", "Rizal"],
    "MIMAROPA": ["Marinduque", "Occidental Mindoro", "Oriental Mindoro", "Palawan", "Romblon"],
    "Region V - Bicol": ["Albay", "Camarines Norte", "Camarines Sur", "Catanduanes",
                         "Masbate", "Sorsogon"],
    "Region VI - Western Visayas": ["Aklan", "Antique", "Capiz", "Guimaras", "Iloilo"],
    "NIR - Negros Island": ["Negros Occidental", "Negros Oriental", "Siquijor"],
    "Region VII - Central Visayas": ["Bohol", "Cebu"],
    "Region VIII - Eastern Visayas": ["Biliran", "Eastern Samar", "Leyte", "Northern Samar",
                                      "Samar", "Southern Leyte"],
    "Region IX - Zamboanga Peninsula": ["Zamboanga Del Norte", "Zamboanga Del Sur",
                                        "Zamboanga Sibugay"],
    "Region X - Northern Mindanao": ["Bukidnon", "Camiguin", "Lanao Del Norte",
                                     "Misamis Occidental", "Misamis Oriental"],
    "Region XI - Davao": ["Davao de Oro (Compos. Valley)", "Davao Del Norte", "Davao Del Sur",
                          "Davao Occidental", "Davao Oriental"],
    "Region XII - SOCCSKSARGEN": ["Cotabato", "Sarangani", "South Cotabato", "Sultan Kudarat"],
    "Caraga": ["Agusan Del Norte", "Agusan Del Sur", "Dinagat Islands", "Surigao Del Norte",
               "Surigao Del Sur"],
    "BARMM": ["Basilan", "Lanao Del Sur", "Maguindanao", "Maguindanao del Sur", "Sulu",
              "Tawi-Tawi"],
    "Nationwide / Other": ["Philippines", "(Independent City)", "Special Geographic Area",
                           "sample province"],
}
_PROVINCE_TO_REGION = {p.lower(): r for r, ps in REGIONS.items() for p in ps}


def region_for(area_of_delivery: Optional[str]) -> str:
    """Area of Delivery can list several provinces separated by commas."""
    if not area_of_delivery:
        return "Unknown"
    regions = []
    for part in re.split(r"[,;/]", area_of_delivery):
        r = _PROVINCE_TO_REGION.get(part.strip().lower())
        if r and r not in regions:
            regions.append(r)
    if not regions:
        return "Unknown"
    return regions[0] if len(regions) == 1 else "Multiple Regions"


NCR_CITIES = ["Caloocan", "Las Pinas", "Las Piñas", "Makati", "Malabon", "Mandaluyong",
              "Manila", "Marikina", "Muntinlupa", "Navotas", "Paranaque", "Parañaque",
              "Pasay", "Pasig", "Pateros", "Quezon City", "San Juan", "Taguig", "Valenzuela"]
_ROMAN_REGION = {
    "I": "Region I - Ilocos", "II": "Region II - Cagayan Valley",
    "III": "Region III - Central Luzon", "IV-A": "Region IV-A - CALABARZON",
    "IVA": "Region IV-A - CALABARZON", "IV-B": "MIMAROPA", "IVB": "MIMAROPA",
    "V": "Region V - Bicol", "VI": "Region VI - Western Visayas",
    "VII": "Region VII - Central Visayas", "VIII": "Region VIII - Eastern Visayas",
    "IX": "Region IX - Zamboanga Peninsula", "X": "Region X - Northern Mindanao",
    "XI": "Region XI - Davao", "XII": "Region XII - SOCCSKSARGEN", "XIII": "Caraga",
    "NCR": "NCR", "CAR": "CAR", "BARMM": "BARMM", "CARAGA": "Caraga",
}
# Longest names first so "Davao Del Norte" wins over "Davao".
_PROVINCES_BY_LEN = sorted(_PROVINCE_TO_REGION, key=len, reverse=True)


def region_from_entity(entity: Optional[str]) -> str:
    """Fallback when Area of Delivery is blank: read the procuring entity name,
    e.g. 'CITY OF CAGAYAN DE ORO, MISAMIS ORIENTAL', 'CITY OF MAKATI',
    'VICENTE SOTTO MEMORIAL MEDICAL CENTER - REGION VII'. Province names are
    only trusted after a comma, ' - ' or 'PROVINCE OF', so 'RIZAL MEDICAL
    CENTER' (in Pasig) is not mistaken for Rizal province."""
    e = (entity or "").strip()
    if not e:
        return "Unknown"
    low = e.lower()
    # 1. province after a separator / "province of"
    for prov in _PROVINCES_BY_LEN:
        if prov in ("philippines", "(independent city)", "special geographic area",
                    "sample province"):
            continue
        if re.search(r"(?:,|\s-\s|province of)\s*%s\b" % re.escape(prov), low):
            return _PROVINCE_TO_REGION[prov]
    # 2. Metro Manila cities
    for city in NCR_CITIES:
        if re.search(r"\b%s\b" % re.escape(city.lower()), low):
            return "NCR"
    # 3. explicit region code
    m = re.search(r"\bregion\s*([ivx]+(?:-?[ab])?|\d+|ncr|car|barmm|caraga)\b", low)
    if m:
        code = m.group(1).upper()
        if code.isdigit():
            romans = ["", "I", "II", "III", "IV-A", "V", "VI", "VII", "VIII", "IX", "X",
                      "XI", "XII", "XIII"]
            code = romans[int(code)] if int(code) < len(romans) else ""
        if code in _ROMAN_REGION:
            return _ROMAN_REGION[code]
    # 4. leading province name for field offices ("DPWH - PAMPANGA 2ND DEO")
    for prov in _PROVINCES_BY_LEN:
        if re.search(r"\s-\s%s\b" % re.escape(prov), low):
            return _PROVINCE_TO_REGION[prov]
    return "Unknown"


# --------------------------------------------------------------------------
# Budget bracket (PHP)
# --------------------------------------------------------------------------
BUDGET_BRACKETS = [
    (1_000_000, "Below ₱1M"),
    (5_000_000, "₱1M – ₱5M"),
    (15_000_000, "₱5M – ₱15M"),
    (50_000_000, "₱15M – ₱50M"),
    (float("inf"), "₱50M and above"),
]


def budget_bracket(budget: Optional[float]) -> str:
    if budget is None:
        return "Not stated"
    for limit, label in BUDGET_BRACKETS:
        if budget < limit:
            return label
    return BUDGET_BRACKETS[-1][1]
