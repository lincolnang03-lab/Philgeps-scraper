"""Offline tests: python -m unittest discover -s tests"""
import json
import unittest
from pathlib import Path

from philgeps import categories as c
from philgeps.scraper import split_category_and_entity

REF = json.loads((Path(__file__).parent.parent / "philgeps" / "site_reference.json")
                 .read_text(encoding="utf-8"))


class TestCoverage(unittest.TestCase):
    def test_every_site_category_maps_to_exactly_one_sector(self):
        mapped = [x for cats in c.SECTORS.values() for x in cats]
        self.assertEqual(len(mapped), len(set(mapped)), "a category is in two sectors")
        missing = [x for x in REF["site_categories"] if c.sector_for_site_category(x) is None]
        self.assertEqual(missing, [])
        extra = sorted(set(mapped) - set(REF["site_categories"]))
        self.assertEqual(extra, [], "mapped category not present on the site")

    def test_every_location_maps_to_a_region(self):
        self.assertEqual([x for x in REF["locations"] if c.region_for(x) == "Unknown"], [])


class TestCategorize(unittest.TestCase):
    def check(self, site_cat, title, expected_sector, expected_source, desc=""):
        self.assertEqual(c.categorize(site_cat, title, desc), (expected_sector, expected_source))

    def test_specific_category_is_trusted(self):
        self.check("Furniture", "supply & delivery of Armchairs",
                   "Furniture, Fixtures & Appliances", "site_category")

    def test_misfiled_vaccine_is_moved_to_health(self):
        self.check("Chemicals and Chemical Products", "1126 HEPA B VACCINE",
                   "Health & Medical", "title_keywords")
        self.check("Chemicals and Chemical Products", "1126 VARIOUS PARENTAL SOLUTIONS",
                   "Health & Medical", "title_keywords")

    def test_vague_category_without_keywords_falls_back(self):
        self.check("Chemicals and Chemical Products", "Chlorine granules",
                   "Industrial & Scientific Equipment", "site_category")

    def test_general_merchandise_uses_keywords(self):
        self.check("General Merchandise", "Procurement of laptops for teachers",
                   "IT & Telecommunications", "title_keywords")

    def test_consulting_is_not_pulled_into_construction(self):
        self.check("Consulting Services",
                   "DETAILED ENGINEERING DESIGN CONSTRUCTION OF MULTI-PURPOSE BUILDING",
                   "Professional & Consulting Services", "site_category")

    def test_food_and_copier_keywords(self):
        self.check("General Merchandise",
                   "Supply and Delivery of Food Supplies for 2026 Palarong Panlalawigan",
                   "Food & Catering", "title_keywords")
        self.check("Services", "Rental of Multi-function Copier Machines",
                   "Office Supplies & Equipment", "title_keywords")
        self.check("General Merchandise", "Procurement of non-food supplies",
                   "General Merchandise & Other", "site_category")

    def test_missing_category(self):
        self.check("", "Construction of farm to market road",
                   "Construction & Infrastructure", "title_keywords")
        self.check(None, "zzz", c.UNCATEGORIZED, "uncategorized")

    def test_description_boilerplate_is_ignored(self):
        long_desc = "x " * 400 + "construction of road"
        self.check("General Merchandise", "assorted items",
                   "General Merchandise & Other", "site_category", desc=long_desc)


class TestDerived(unittest.TestCase):
    def test_region(self):
        self.assertEqual(c.region_for("Pampanga"), "Region III - Central Luzon")
        self.assertEqual(c.region_for("Metro Manila"), "NCR")
        self.assertEqual(c.region_for("Cebu, Bohol"), "Region VII - Central Visayas")
        self.assertEqual(c.region_for("Cebu, Metro Manila"), "Multiple Regions")
        self.assertEqual(c.region_for(""), "Unknown")

    def test_region_from_entity(self):
        f = c.region_from_entity
        self.assertEqual(f("CITY OF CAGAYAN DE ORO, MISAMIS ORIENTAL"), "Region X - Northern Mindanao")
        self.assertEqual(f("CITY OF MAKATI"), "NCR")
        self.assertEqual(f("PNOC EXPLORATION CORPORATION - TAGUIG CITY"), "NCR")
        self.assertEqual(f("VICENTE SOTTO MEMORIAL MEDICAL CENTER - REGION VII"),
                         "Region VII - Central Visayas")
        self.assertEqual(f("PHILIPPINE HEALTH INSURANCE CORPORATION - REGION III"),
                         "Region III - Central Luzon")
        self.assertEqual(f("PROVINCE OF EASTERN SAMAR"), "Region VIII - Eastern Visayas")
        self.assertEqual(f("CITY OF MATI, PROVINCE OF DAVAO ORIENTAL"), "Region XI - Davao")
        self.assertEqual(f("DEPARTMENT OF PUBLIC WORKS AND HIGHWAYS - PAMPANGA 2ND DEO"),
                         "Region III - Central Luzon")
        self.assertEqual(f("MUNICIPALITY OF SAN JUAN, BATANGAS"), "Region IV-A - CALABARZON")
        # not fooled by a province name used as a hospital name
        self.assertEqual(f("RIZAL MEDICAL CENTER"), "Unknown")
        self.assertEqual(f("NATIONAL POWER CORPORATION"), "Unknown")

    def test_budget(self):
        self.assertEqual(c.budget_bracket(None), "Not stated")
        self.assertEqual(c.budget_bracket(987_485), "Below ₱1M")
        self.assertEqual(c.budget_bracket(8_227_931.27), "₱5M – ₱15M")
        self.assertEqual(c.budget_bracket(50_000_000), "₱50M and above")


class TestListingParse(unittest.TestCase):
    def test_split_with_commas_in_both_parts(self):
        self.assertEqual(
            split_category_and_entity(", Agricultural Products (Seeds, Seedlings, Plants..) ,CITY OF BATANGAS, BATANGAS"),
            ("Agricultural Products (Seeds, Seedlings, Plants..)", "CITY OF BATANGAS, BATANGAS"))
        self.assertEqual(
            split_category_and_entity(", Construction Materials and Supplies ,CITY OF ANGELES, PAMPANGA"),
            ("Construction Materials and Supplies", "CITY OF ANGELES, PAMPANGA"))

    def test_unknown_category_falls_back_to_separator(self):
        self.assertEqual(split_category_and_entity(", Brand New Category ,SOME AGENCY"),
                         ("Brand New Category", "SOME AGENCY"))


if __name__ == "__main__":
    unittest.main()
