import tempfile
import unittest
from pathlib import Path

from docx import Document
from content_parser import read_content
from generate_xml import GeneratorError
from generate_simple_xml import parse_ordered_script, parse_content_anchors
from generate_simple_xml_v3 import inspect_docx_components, parse_docx_video_cuts


class ContentFormatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'content.docx'

    def table(self, rows):
        doc = Document()
        table = doc.add_table(rows=0, cols=len(rows[0]))
        for values in rows:
            for cell, value in zip(table.add_row().cells, values):
                cell.text = value
        doc.save(self.path)
        return read_content(self.path)

    def test_legacy_and_new_intro_identical(self):
        def fixture(prefix):
            return self.table([
                ('Video', 'Source title'), ('INTRO', 'Opening narration'),
                ('INTRO', prefix + 'Vid1: 6:23 - 6:27 + 11:30 - 11:34'),
                ('INTRO', 'vid2 : 1:53 - 1:58 + 3:44 - 3:49'),
                ('Vào tình huống 1', '8:07 - 8:13'),
                ('Vào tình huống 1', '8:07 - 8:13'),
                ('Nội dung', 'Body narration'), ('Nội dung', '0:25 - 1:24'),
                ('Kết bài', 'Ending narration')])
        old = fixture('Đoạn kịch tính ')
        new = fixture('')
        projection = lambda es: [(e.kind, e.source, e.start, e.end, e.section)
                                  for e in es]
        self.assertEqual(projection(old), projection(new))
        tokens, lengths = parse_ordered_script(self.path, 60)
        words, anchors = parse_content_anchors(self.path, 60)
        cuts = parse_docx_video_cuts(self.path)
        components = inspect_docx_components(self.path)
        self.assertEqual(len(cuts), 6)
        self.assertEqual(len(anchors), 6)
        self.assertEqual(len(lengths), 3)
        self.assertEqual(sum(c.video_cut_count for c in components), 6)
        self.assertEqual(sum(c.text_block_count for c in components), 3)
        self.assertEqual([a.word_position for a in anchors], [2, 2, 2, 2, 2, 4])

    def test_aliases_and_unicode_timecodes(self):
        for label in ['TH1', 'Tình huống 1', 'Tinh huong 1', 'Scenario 1', 'Case #1']:
            with self.subTest(label=label):
                es = self.table([('Intro', 'Hello'), (label, '00:01:02,500 → 00:01:04,000'.replace('→', '—'))])
                cut = next(e for e in es if e.kind == 'video')
                self.assertEqual((cut.source, cut.start, cut.end), (1, 62.5, 64))

    def test_multiple_sources_in_one_cell(self):
        es = self.table([('Intro', 'Hello'), ('Intro', 'Video 1: 0:01 to 0:03 + Vid2: 0:04 --> 0:06')])
        self.assertEqual([e.source for e in es if e.kind == 'video'], [1, 2])

    def test_paragraphs_and_table_in_document_order(self):
        doc = Document()
        for text in ['Document title', 'Introduction', 'Opening', 'TH1', '0:01 – 0:03', 'Body']:
            doc.add_paragraph(text)
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = 'Nội dung'
        table.cell(0, 1).text = '0:04 - 0:06'
        doc.add_paragraph('Conclusion')
        doc.add_paragraph('Ending')
        doc.save(self.path)
        es = read_content(self.path)
        self.assertEqual([e.kind for e in es if e.kind != 'section'],
                         ['audio', 'video', 'audio', 'video', 'audio'])

    def test_header_columns_and_notes(self):
        es = self.table([('STT', 'Section', 'Content', 'Notes'),
                         ('1', 'Intro', 'Hello', 'Do not read'),
                         ('2', 'TH1', '0:01 - 0:03', '0:40 - 0:50')])
        self.assertEqual([e.text for e in es if e.kind == 'audio'], ['Hello'])
        self.assertEqual(len([e for e in es if e.kind == 'video']), 1)

    def test_ambiguous_and_invalid_fail_in_preview_too(self):
        for text in ['0:01 - 0:03', 'Vid7: 0:01 - 0:03', 'Vid1: 0:03 - 0:01',
                     'Vid1: 0:65 - 1:10', 'Vid1: 0:01 - broken',
                     'Vid1 Vid2: 0:01 - 0:03']:
            with self.subTest(text=text):
                with self.assertRaises(GeneratorError):
                    self.table([('Intro', 'Hello'), ('Intro', text)])
                with self.assertRaises(GeneratorError):
                    inspect_docx_components(self.path)

    def test_intentional_body_repeats_preserved(self):
        es = self.table([('TH1', '0:01 - 0:03'), ('Nội dung', 'Hello'),
                         ('Nội dung', '0:01 - 0:03'), ('Nội dung', '0:01 - 0:03')])
        self.assertEqual(len([e for e in es if e.kind == 'video']), 3)


if __name__ == '__main__':
    unittest.main()
