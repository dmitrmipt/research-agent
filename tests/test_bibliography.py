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
        self.assertNotIn("title", refs[0])
        verbatim = [{**contextual[0], "text": "B. R. Mollow, Power spectrum of light, Physical Review 188, 1969 (1969)."}]
        apply_reference_hints(refs, verbatim, [{"reference_id": "r1", "title": "Power spectrum of light",
                                               "evidence": "Power spectrum of light", "search_query": "Mollow 188 1969"}])
        self.assertEqual(refs[0]["title"], "Power spectrum of light")
        self.assertEqual(refs[0]["title_source"], "Текст статьи")

    def test_labelled_references_keep_year_lines_and_all_mentions(self):
        parts = split_sections([
            "Introduction\nFirst [Ber26a; BCG+\n23]. Again [Ber26a, Lemma 4]. Unknown [XBer26a].",
            "References\n[Ber26a] Alice Reader. First result,\n2026. URL: https://example.org/first.\n2",
            "[BCG+23] Bob Author. Second result. Journal, 2023.",
        ])
        first, second = bibliography(parts)
        self.assertEqual((first["number"], second["number"]), ("Ber26a", "BCG+23"))
        self.assertIn("2026. URL:", first["text"])
        self.assertEqual(len({loc["anchor"] for loc in first["citations"]}), 2)
        self.assertEqual(len(second["citations"]), 1)

    def test_wrapped_year_and_caption_do_not_create_or_pollute_numbered_entries(self):
        parts = split_sections(["References\n[1] Alice Reader. First title. Journal, 4:11,\n2020. doi:10.1234/first.\n"
                                "TABLE XVI. A table drawn between the references.\nUnrelated row\n[2] Bob Writer. Second title. Journal, 2021."])
        first, second = bibliography(parts)
        self.assertEqual([first["number"], second["number"]], ["1", "2"])
        self.assertIn("2020.", first["text"])
        self.assertNotIn("Unrelated row", first["text"])

    def test_unheaded_numbered_bibliography_does_not_invent_titles(self):
        refs = bibliography(split_sections(["Conclusion\nThe calculation is complete.\n"
            "1) A. Reader and Y . Author: Phys. Rev. Lett.96\n(2006) 060503.\n"
            "2) B. Writer: arXiv:2610.00123.\n3) C. Editor: J. Phys. Soc. Jpn.5(1950) 435."]))
        self.assertEqual(len(refs), 3)
        self.assertTrue(all(_reference_item(ref)["missing_title"] for ref in refs))
        self.assertEqual(_reference_item(refs[0])["authors"], "A. Reader and Y. Author")
        chapters = split_sections(["1. Introduction\nEarlier work in 2000.\n2. Methods\nData from 2020.\n3. Results\nMeasurements in 2021."])
        self.assertEqual(bibliography(chapters), [])

    def test_reference_formats_separate_names_title_and_publication(self):
        examples = (
            ("Álvaro M. Alhambra. A quantum experiment. PRX Quantum, 4:040201, 2023.doi:10.1234/test.",
             "Álvaro M. Alhambra", "A quantum experiment", "PRX Quantum, 4:040201 (2023)"),
            ("Schuld M, Killoran N. Quantum Machine Learning in Feature Hilbert Spaces. Physical Review Letters. 2019;122(4):040504.",
             "Schuld M, Killoran N", "Quantum Machine Learning in Feature Hilbert Spaces", "Physical Review Letters, 122(4):040504 (2019)"),
            ('F. Tanaka and F. Komaki, “Bayesian predictive density operators,”Phys. Rev. A, vol. 71, 052323, 2005.doi:10.1234/test.',
             "F. Tanaka and F. Komaki", "Bayesian predictive density operators", "Phys. Rev. A, vol. 71, 052323 (2005)"),
            ("R. Alicki, M. Horodecki, and P. Horodecki. Thermal stability, 2008. URL: https://example.org/paper.",
             "R. Alicki, M. Horodecki, and P. Horodecki", "Thermal stability", "(2008)"),
            ("Gulrajani I, Lopez-Paz D. In Search of Lost Domain Generalization. In: International Conference on Learning Representations; 2021. Available from: https://example.org/paper.",
             "Gulrajani I, Lopez-Paz D", "In Search of Lost Domain Generalization", "In: International Conference on Learning Representations (2021)"),
            ("Victor Bapst et al. Unveiling the predictive power of static structure. Nature Physics, 16:448–454, 2020.",
             "Victor Bapst et al", "Unveiling the predictive power of static structure", "Nature Physics, 16:448–454 (2020)"),
        )
        for source, authors, title, publication in examples:
            with self.subTest(title=title):
                item = _reference_item(entry(source))
                self.assertEqual((item["authors"], item["label"], item["text"]), (authors, title, publication))

    def test_url_and_doi_wrapping_and_confirmed_pdf_priority(self):
        self.assertEqual(reference_text("https:\n//arxiv.\norg/abs/2610.\n12345"), "https://arxiv.org/abs/2610.12345")
        self.assertEqual(reference_text("doi:10.1038/\ns41586-025-09583-x"), "doi:10.1038/s41586-025-09583-x")
        ref = entry("A. Reader: Phys. Rev. Lett.96 (2006) 060503.")
        source = {"DOI": "10.1234/test", "title": ["Verified publication"], "author": [{"family": "Reader"}],
                  "published": {"date-parts": [[2006]]}, "volume": "96", "page": "060503",
                  "short-container-title": ["Phys. Rev. Lett."],
                  "link": [{"content-type": "application/pdf", "URL": "https://example.org/fulltext"}]}
        http = Mock()
        http.json.return_value = {"message": {"items": [source]}}
        self.assertEqual(resolve_reference_titles(Publications(), http, [ref], Mock()), [])
        self.assertEqual(ref["title"], "Verified publication")
        self.assertEqual(ref["dois"], ["10.1234/test"])
        self.assertEqual(ref["url"], "https://example.org/fulltext")

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
