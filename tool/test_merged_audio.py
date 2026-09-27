import tempfile
import math
import struct
import wave
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

from docx import Document

import generate_simple_xml as generator
from generate_xml import GeneratorError, MediaInfo, ScriptToken


class MergedAudioTests(unittest.TestCase):
    def setUp(self):
        self.text = 'one two three four five six seven eight nine ten'.split()
        self.words = [generator.TimedWord(word, i, i + 0.8)
                      for i, word in enumerate(self.text)]
        self.media = MediaInfo(Path('merged.mp3'), 10, 600)
        self.anchor = generator.VideoAnchor(
            5, 0, 'test', ScriptToken('video', th_number=1, source_out=60))

    def align(self, words=None, anchors=None):
        return generator.align_merged_audio(
            self.text, anchors if anchors is not None else [self.anchor],
            self.words if words is None else words, self.media, 60)

    def test_exact_and_source_coverage(self):
        tokens = self.align()
        self.assertEqual([t.kind for t in tokens], ['audio', 'video', 'audio'])
        self.assertEqual((tokens[0].source_in, tokens[0].source_out), (0, 294))
        self.assertEqual((tokens[2].source_in, tokens[2].source_out), (294, 600))

    def test_substitution_and_missing_word(self):
        words = self.words.copy()
        words[1] = generator.TimedWord('wrong', 1, 1.8)
        del words[7]
        self.assertEqual(self.align(words)[0].source_out, 294)

    def test_substitution_at_boundary(self):
        words = self.words.copy()
        words[5] = generator.TimedWord('wrong', 5, 5.8)
        self.assertEqual(self.align(words)[0].source_out, 294)

    def test_unrelated_speech_rejected(self):
        with self.assertRaises(GeneratorError):
            self.align([generator.TimedWord('wrong', i, i + .8) for i in range(10)])

    def test_one_unmatched_region_rejected(self):
        with self.assertRaises(GeneratorError):
            self.align(self.words[:5])

    def test_invalid_timestamp_rejected(self):
        for bad in [float('nan'), -1, 12]:
            with self.subTest(bad=bad), self.assertRaises(GeneratorError):
                self.align([generator.TimedWord('one', bad, bad + 1)] + self.words[1:])

    def test_consecutive_and_edge_video_cuts(self):
        anchors = [generator.VideoAnchor(p, i, 'test', self.anchor.token)
                   for i, p in enumerate([0, 5, 5, 10])]
        self.assertEqual([t.kind for t in self.align(anchors=anchors)],
                         ['video', 'audio', 'video', 'video', 'audio', 'video'])

    def test_asr_word_timestamp_options(self):
        model = unittest.mock.Mock()
        model.transcribe.return_value = (
            iter([SimpleNamespace(end=1, words=[SimpleNamespace(
                word=' Xin chào!', start=0.1, end=1)])]),
            SimpleNamespace(language='vi'))
        module = SimpleNamespace(WhisperModel=unittest.mock.Mock(return_value=model))
        with patch.dict('sys.modules', {'faster_whisper': module}):
            words = generator.transcribe_merged_audio(Path('audio.mp3'), 'medium', 'vi')
        self.assertEqual([w.text for w in words], ['xin', 'chào'])
        self.assertTrue(model.transcribe.call_args.kwargs['word_timestamps'])
        self.assertEqual(model.transcribe.call_args.kwargs['language'], 'vi')

    def test_cut_in_real_silence_when_asr_absorbs_pause(self):
        before = generator.TimedWord('end', 0, 1.9)
        after = generator.TimedWord('next', 2, 3)
        cut = generator.silence_cut_time(before, after, [(1.7, 2.1)])
        self.assertAlmostEqual(cut, 1.9)
        self.assertNotEqual(cut, (before.end + after.start) / 2)

    def test_distant_silence_does_not_move_cut(self):
        before = generator.TimedWord('end', 0, 1)
        after = generator.TimedWord('next', 1.1, 2)
        self.assertAlmostEqual(generator.silence_cut_time(
            before, after, [(0.1, 0.5), (1.5, 1.8)]), 1.05)

    def test_waveform_silence_detection_and_xml_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.wav'
            rate = 16000
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                samples = [int(12000 * math.sin(2 * math.pi * 440 * i / rate))
                           if i < rate or i >= 2 * rate else 0
                           for i in range(3 * rate)]
                audio.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
            silences = generator.detect_audio_silences(path)
            self.assertEqual(len(silences), 1)
            self.assertAlmostEqual(silences[0][0], 1, places=2)
            self.assertAlmostEqual(silences[0][1], 2, places=2)
            words = [generator.TimedWord('first', 0, 1.9),
                     generator.TimedWord('second', 2, 3)]
            anchor = generator.VideoAnchor(1, 0, 'test', self.anchor.token)
            tokens = generator.align_merged_audio(
                ['first', 'second'], [anchor], words,
                MediaInfo(path, 3, 180), 60, silences)
            self.assertEqual(tokens[0].source_out, 90)
            self.assertEqual(tokens[2].source_in, 90)
            self.assertEqual(tokens[2].source_out, 180)

    def test_generation_and_legacy_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'merged.mp3'
            video = root / '1.mp4'
            audio.touch()
            video.touch()
            media = MediaInfo(audio, 10, 600)
            videos = {i: MediaInfo(video, 10, 600) for i in range(1, 7)}
            doc = Document()
            table = doc.add_table(rows=3, cols=2)
            for row, values in zip(table.rows, [
                ('Intro', 'one two three four five'),
                ('Vào tình huống 1', '00:00 - 00:01'),
                ('Lời dẫn', 'six seven eight nine ten'),
            ]):
                for cell, value in zip(row.cells, values):
                    cell.text = value
            content = root / 'content.docx'
            doc.save(content)
            output = root / 'out.xml'
            kwargs = dict(project_dir=root, content=content, video_dir=root,
                          audio_dir=root, output=output)
            with patch.object(generator, 'discover_media', return_value=(videos, [media])), \
                 patch.object(generator, 'probe_stream_details', return_value=generator.StreamDetails()), \
                 patch.object(generator, 'detect_audio_silences', return_value=[]), \
                 patch.object(generator, 'transcribe_merged_audio', return_value=self.words) as asr:
                summary = generator.generate_simple_project_xml(**kwargs)
                self.assertEqual(summary.narration_count, 2)
                clips = ET.parse(output).findall('./sequence/media/audio/track')[1].findall('clipitem')
                self.assertEqual([(c.findtext('in'), c.findtext('out')) for c in clips],
                                 [('0', '294'), ('294', '600')])
                self.assertEqual([(c.findtext('start'), c.findtext('end')) for c in clips],
                                 [('0', '294'), ('354', '660')])
                self.assertEqual(clips[0].find('file').get('id'), clips[1].find('file').get('id'))
                self.assertEqual(summary.duration_seconds, 11)
                asr.reset_mock()
                with patch.object(generator, 'discover_media', return_value=(videos, [media, media])):
                    summary = generator.generate_simple_project_xml(**kwargs)
                    self.assertEqual(summary.duration_seconds, 21)
                    asr.assert_not_called()
                transcript = root / 'transcript.txt'
                transcript.write_text('1\n00:00:00,000 --> 00:00:05,000\none two three four five\n')
                generator.generate_simple_project_xml(**kwargs, transcript=transcript)
                asr.assert_not_called()


if __name__ == '__main__':
    unittest.main()
