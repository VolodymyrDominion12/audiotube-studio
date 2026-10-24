import unittest
from unittest import mock

from audiotube.media import FFmpegError, ffprobe_bin, has_ffmpeg


class FFprobeResolutionTests(unittest.TestCase):
    def test_prefers_ffprobe(self):
        with mock.patch("audiotube.media.shutil.which", return_value="/usr/bin/ffprobe"):
            self.assertEqual(ffprobe_bin(), "/usr/bin/ffprobe")

    def test_snap_fallback(self):
        def which(name):
            return "/snap/bin/ffmpeg.ffprobe" if name == "ffmpeg.ffprobe" else None

        with mock.patch("audiotube.media.shutil.which", side_effect=which):
            self.assertEqual(ffprobe_bin(), "/snap/bin/ffmpeg.ffprobe")

    def test_missing(self):
        with mock.patch("audiotube.media.shutil.which", return_value=None):
            with self.assertRaises(FFmpegError):
                ffprobe_bin()
            self.assertFalse(has_ffmpeg())


if __name__ == "__main__":
    unittest.main()
