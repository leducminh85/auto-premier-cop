import tempfile
import unittest
from pathlib import Path

from docx import Document
from content_parser import read_content, ContentParseError
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

    def test_source_touching_timestamp(self):
        for label in ['Vid2', 'vid 2', 'Video 2', 'TH2', 'Case #2', 'Tình huống 2']:
            with self.subTest(label=label):
                text = f'Đoạn kịch tính {label}:2:06 - 2:12 + 4:39 - 4:42 + Vid1:00:01:02,500 → 00:01:04,000'
                es = self.table([('Video', text)])
                cuts = [e for e in es if e.kind == 'video']
                self.assertEqual([(e.source, e.start, e.end) for e in cuts],
                                 [(2, 126, 132), (2, 279, 282), (1, 62.5, 64)])
                self.assertTrue(all(e.text == text for e in cuts))
                self.assertEqual(len(parse_docx_video_cuts(self.path)), 3)
                self.assertEqual(sum(c.video_cut_count for c in inspect_docx_components(self.path)), 3)

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

    def test_timestamp_whitespace_in_all_consumers(self):
        for body in ['1:23-4:22', '1:23 -4:22', '1:23 - 4: 22',
                     '1 : 23 - 4 : 22', '1:\u00a023\t-4:\u202f22',
                     '00 : 01 : 23 - 00: 04: 22']:
            for prefix in ['', 'Vid1:', 'Vid1: ']:
                with self.subTest(body=body, prefix=prefix):
                    text = prefix + body
                    es = self.table([('Intro', 'Opening narration'), ('TH1', text)])
                    cuts = [e for e in es if e.kind == 'video']
                    self.assertEqual([(e.source, e.start, e.end) for e in cuts], [(1, 83, 262)])
                    self.assertEqual(cuts[0].text, text)
                    self.assertEqual(len(parse_docx_video_cuts(self.path)), 1)
                    self.assertEqual(sum(c.video_cut_count for c in inspect_docx_components(self.path)), 1)
                    self.assertEqual(len(parse_content_anchors(self.path, 60)[1]), 1)
                    self.assertEqual(len(parse_ordered_script(self.path, 60)[0]), 2)
        es = self.table([('Intro', 'Vid2:1: 34-2: 00 + Vid1:00: 02: 03,500-00: 02: 04,500')])
        self.assertEqual([(e.source, e.start, e.end) for e in es if e.kind == 'video'],
                         [(2, 94, 120), (1, 123.5, 124.5)])

    def test_whitespace_does_not_hide_invalid_times(self):
        for text in ['Vid1: 0: 65 - 1: 10', 'Vid1: 1: 20 - 1: 10',
                     'Vid1: 0: 10 - broken', 'Vid7: 0: 10 - 0: 20',
                     'Vid1: 0: 1 - 0: 20', 'Vid1: 0: 1 0 - 0: 20']:
            with self.subTest(text=text), self.assertRaises(GeneratorError):
                self.table([('Intro', text)])
        with self.assertRaises(ContentParseError) as caught:
            self.table([('Intro', 'Vid1: 0 : 65 - 1: 10')])
        error = caught.exception
        self.assertEqual(error.text[slice(*error.span)], '0 : 65')

    def test_header_columns_and_notes(self):
        es = self.table([('STT', 'Section', 'Content', 'Notes'),
                         ('1', 'Intro', 'Hello', 'Do not read'),
                         ('2', 'TH1', '0:01 - 0:03', '0:40 - 0:50')])
        self.assertEqual([e.text for e in es if e.kind == 'audio'], ['Hello'])
        self.assertEqual(len([e for e in es if e.kind == 'video']), 1)

    def test_ambiguous_and_invalid_fail_in_preview_too(self):
        for text in ['0:01 - 0:03', 'Vid7: 0:01 - 0:03', 'Vid1: 0:03 - 0:01',
                     'Vid1: 0:65 - 1:10', 'Vid1: 0:01 - broken',
                     'Vid1 Vid2: 0:01 - 0:03', 'Vid1 Vid2:0:01 - 0:03',
                     'Vid7:0:01 - 0:03', 'Vid1:0:65 - 1:10',
                     'Vid1:0:03 - 0:01', 'Vid1:0:01 - broken']:
            with self.subTest(text=text):
                with self.assertRaises(GeneratorError):
                    self.table([('Intro', 'Hello'), ('Intro', text)])
                with self.assertRaises(GeneratorError):
                    inspect_docx_components(self.path)

    def test_diagnostics_preserve_original_and_identify_bad_segment(self):
        for bad, highlighted in [('Vid1: 0:10 - broken', 'Vid1: 0:10 - broken'),
                                 ('Vid1: 0:65 - 1:10', '0:65'),
                                 ('Vid1: 0:20 - 0:10', 'Vid1: 0:20 - 0:10')]:
            text = 'Vid2: 0:01 - 0:03 + ' + bad
            with self.subTest(bad=bad):
                with self.assertRaises(ContentParseError) as caught:
                    self.table([('Intro', text)])
                error = caught.exception
                self.assertEqual(error.text, text)
                self.assertEqual(error.location, 'bảng 1, dòng 1')
                self.assertEqual(error.path, self.path)
                self.assertEqual(text[slice(*error.span)], highlighted)
                self.assertTrue(error.hint)
                self.assertIn(text, str(error))

    def test_intentional_body_repeats_preserved(self):
        es = self.table([('TH1', '0:01 - 0:03'), ('Nội dung', 'Hello'),
                         ('Nội dung', '0:01 - 0:03'), ('Nội dung', '0:01 - 0:03')])
        self.assertEqual(len([e for e in es if e.kind == 'video']), 3)


if __name__ == '__main__':
    unittest.main()
