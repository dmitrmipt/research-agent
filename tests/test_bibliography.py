import unittest
from unittest.mock import Mock

from bibliographic import reference_text, vocabulary
from materials import _reference_item, split_sections
from publications import Publications, _citation_words, _matches_citation, crossref_record, metadata_title
from research import apply_reference_hints, bibliography, reference_inputs, resolve_reference_titles


def entry(text, number="1"):
    return {"text": text, "number": number, "page": 1, "url": "", "arxiv_ids": [], "dois": []}


class BibliographyTest(unittest.TestCase):
    def test_publisher_title_mathml_is_rendered_as_readable_text(self):
        title = 'Specific Heat of<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:msup><mml:mi>Mg</mml:mi><mml:mn>11</mml:mn></mml:msup><mml:msub><mml:mi>B</mml:mi><mml:mn>2</mml:mn></mml:msub></mml:math>: Evidence'
        self.assertEqual(metadata_title(title), "Specific Heat of Mg¹¹B₂: Evidence")

    def test_authors_and_journal_are_never_a_missing_title_fallback(self):
        text = "F. Bouquet, R. A. Fisher, and J. D. Jorgensen, Phys. Rev. Lett.87, 047001 (2001)."
        item = _reference_item(entry(text))
        self.assertEqual(item["label"], "Название не установлено")
        self.assertTrue(item["missing_title"])
        self.assertEqual(item["authors"], "F. Bouquet, R. A. Fisher, and J. D. Jorgensen")
        self.assertEqual(item["text"], "Phys. Rev. Lett.87, 047001 (2001)")

    def test_real_title_after_initialled_authors_is_not_consumed_as_author_year(self):
        title = "Symmetry breaking and skyrmionic transport in twisted bilayer graphene"
        item = _reference_item(entry("S. Chatterjee, N. Bultinck, and M. P. Zaletel, " + title + ", Physical Review B101, 165141 (2020)."))
        self.assertEqual(item["label"], title)
        self.assertEqual(item["authors"], "S. Chatterjee, N. Bultinck, and M. P. Zaletel")
        self.assertEqual(item["text"], "Physical Review B101, 165141 (2020)")

    def test_line_wraps_are_joined_but_scientific_hyphens_are_retained(self):
        words = vocabulary("Superconductivity and topological electron phonon states.")
        self.assertEqual(reference_text("Superconduc-\ntivity in topolog- ical systems", words), "Superconductivity in topological systems")
        self.assertEqual(reference_text("electron-\nphonon and first-\norder transitions", words), "electron-phonon and first-order transitions")
        self.assertEqual(reference_text("super\u00adconductivity", words), "superconductivity")
        self.assertEqual(reference_text("well-known spin-orbit coupling", words), "well-known spin-orbit coupling")
        self.assertEqual(reference_text("Type-\nI and spin-\n1/2", words), "Type-I and spin-1/2")
        self.assertEqual(reference_text("https://example.org/long-\nname"), "https://example.org/long-name")
        self.assertEqual(reference_text("10.1234/long-\nname"), "10.1234/long-name")

    def test_bibliography_repairs_before_flattening_and_keeps_citation_pages(self):
        parts = split_sections(["Introduction\nSuperconductivity is topological [1].\nReferences\n"
                                "[1] A. Reader, Superconduc-\ntivity in a topolog-\nical electron-\nphonon system, Journal of Tests, 2020."])
        refs = bibliography(parts)
        self.assertIn("Superconductivity in a topological electron-phonon system", refs[0]["text"])
        citation, = refs[0]["citations"]
        self.assertEqual((citation["page"], citation["section"]), (1, "Введение"))
        self.assertIn("is topological [1]", citation["context"])

    def test_model_hints_require_a_verbatim_title_and_do_not_supply_links(self):
        refs = [entry("B. R. Mollow, Physical Review 188, 1969 (1969).")]
        inputs = reference_inputs(refs, [])
        apply_reference_hints(refs, inputs, [{"reference_id": "r1", "title": "Invented title",
                                             "evidence": "Physical Review 188", "search_query": "Mollow Physical Review 188 1969"}])
        self.assertNotIn("title", refs[0])
        self.assertEqual(refs[0]["url"], "")
        self.assertEqual(refs[0]["search_query"], "Mollow Physical Review 188 1969")
        contextual = [{"reference_id": "r1", "text": refs[0]["text"], "context": "The cited work Power spectrum of light [1]."}]
        apply_reference_hints(refs, contextual, [{"reference_id": "r1", "title": "Power spectrum of light",
                                                 "evidence": "The cited work Power spectrum of light [1].", "search_query": "Mollow 188 1969"}])
        self.assertEqual(refs[0]["title"], "Power spectrum of light")
        self.assertEqual(refs[0]["title_source"], "Текст статьи")

    def test_journal_abbreviations_need_matching_author_volume_page_and_year(self):
        data = {"DOI": "10.1234/bouquet", "title": ["A verified superconductivity paper"],
                "author": [{"family": "Bouquet"}], "container-title": ["Physical Review Letters"],
                "volume": "87", "page": "047001-047005", "published": {"date-parts": [[2001]]}}
        citation = " " + _citation_words("F. Bouquet, Phys. Rev. Lett.87, 047001 (2001).") + " "
        self.assertTrue(_matches_citation(citation, data, crossref_record(data)))
        for field, value in (("volume", "88"), ("page", "047002"), ("container-title", ["Physical Review B"])):
            wrong = {**data, field: value}
            self.assertFalse(_matches_citation(citation, wrong, crossref_record(wrong)))

    def test_crossref_citation_skips_records_without_titles(self):
        http = Mock()
        match = {"DOI": "10.1103/physrev.188.1969", "title": ["Power spectrum of light scattered by two-level systems"],
                 "author": [{"family": "Mollow"}], "container-title": ["Physical Review"],
                 "volume": "188", "page": "1969-1975", "published": {"date-parts": [[1969]]}}
        http.json.return_value = {"message": {"items": [
            {"DOI": "10.1234/missing-title"}, {"DOI": "10.1234/empty-title", "title": []}, match,
        ]}}
        record = Publications().crossref_citation(http, "B. R. Mollow, Physical Review 188, 1969 (1969).")
        self.assertIsNotNone(record)
        self.assertEqual(record["doi"], match["DOI"])
        self.assertEqual(record["title"], match["title"][0])

    def test_title_lookup_is_not_limited_to_first_three_references(self):
        refs = [entry(f"A. Reader, Journal of Tests 12, {index} (2020).", str(index)) for index in range(1, 7)]
        service, http, updated = Mock(), Mock(), Mock()
        http.checkpoint.return_value = 100

        def lookup(http, citation, query):
            index = citation.split(", ")[2].split()[0]
            return {"arxiv_id": "", "doi": "10.1234/test-" + index, "title": "Verified title " + index,
                    "authors": ["A. Reader"], "published": "2020", "source": "Crossref", "url": "https://doi.org/10.1234/test-" + index}

        service.crossref_citation.side_effect = lookup
        self.assertEqual(resolve_reference_titles(service, http, refs, updated), [])
        self.assertEqual(service.crossref_citation.call_count, 6)
        self.assertEqual(updated.call_count, 6)
        self.assertTrue(all(ref["title"].startswith("Verified title") and ref["url"].startswith("https://doi.org/") for ref in refs))


if __name__ == "__main__":
    unittest.main()
