from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

os.environ["METADATA_CLEANER_ALLOWED_TAILSCALE_USER"] = "owner"
os.environ["METADATA_CLEANER_TAILSCALE_HOST"] = "cleaner.example.invalid:3495"

import app


class MetadataCleanerTests(unittest.TestCase):
    def make_jpeg(self, root: Path) -> Path:
        source = root / "photo.jpg"
        Image.new("RGB", (64, 48), "#6a8c77").save(source, quality=95)
        subprocess.run(
            [
                "/usr/bin/exiftool", "-overwrite_original",
                "-Artist=INERT_PERSON_SENTINEL", "-Comment=INERT_COMMENT_SENTINEL",
                "-GPSLatitude=60.1699", "-GPSLatitudeRef=N",
                "-GPSLongitude=24.9384", "-GPSLongitudeRef=E", str(source),
            ],
            check=True, capture_output=True,
        )
        return source

    def add_image_metadata(self, source: Path) -> Path:
        subprocess.run(
            [
                "/usr/bin/exiftool", "-overwrite_original",
                "-Artist=INERT_PERSON_SENTINEL", "-XMP-dc:Creator=INERT_XMP_SENTINEL",
                "-Comment=INERT_COMMENT_SENTINEL",
                "-GPSLatitude=60.1699", "-GPSLatitudeRef=N", str(source),
            ],
            check=True, capture_output=True,
        )
        return source

    def assert_clean_image(self, source: Path, root: Path, media_type: str) -> Path:
        job = root / "job"
        job.mkdir()
        output, actual_type, report = app.clean_image(source, job)
        self.assertEqual(actual_type, media_type)
        self.assertEqual(report["report"]["verification"]["status"], "passed")
        self.assertEqual(app.exif_field_names(output, strict=True), [])
        raw = output.read_bytes()
        for sentinel in (b"INERT_PERSON_SENTINEL", b"INERT_XMP_SENTINEL", b"INERT_COMMENT_SENTINEL"):
            self.assertNotIn(sentinel, raw)
        removed = {item["field"] for item in report["report"]["removed"]["fields"]}
        self.assertFalse(any(field.endswith((":ImageWidth", ":ImageHeight")) for field in removed))
        return output

    def test_plain_and_tagged_png_pass_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "plain.png"
            Image.new("RGB", (16, 12), "#6a8c77").save(plain)
            self.assert_clean_image(plain, root, "image/png")
            tagged = root / "tagged.png"
            Image.new("RGBA", (16, 12), (106, 140, 119, 128)).save(tagged)
            self.add_image_metadata(tagged)
            shutil.rmtree(root / "job")
            output = self.assert_clean_image(tagged, root, "image/png")
            with Image.open(output) as cleaned:
                self.assertEqual(cleaned.getpixel((0, 0)), (106, 140, 119, 128))

    def test_plain_and_tagged_webp_pass_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "plain.webp"
            Image.new("RGB", (16, 12), "#6a8c77").save(plain, lossless=True)
            self.assert_clean_image(plain, root, "image/webp")
            tagged = root / "tagged.webp"
            Image.new("RGBA", (16, 12), (106, 140, 119, 128)).save(tagged, lossless=True)
            self.add_image_metadata(tagged)
            shutil.rmtree(root / "job")
            self.assert_clean_image(tagged, root, "image/webp")

    def test_palette_png_keeps_transparent_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "palette.png"
            palette = Image.new("P", (16, 12), 0)
            palette.putpalette([255, 0, 0, 0, 255, 0])
            palette.putpixel((1, 1), 1)
            palette.save(source, transparency=0)
            with Image.open(source) as opened:
                self.assertEqual(opened.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 0))
            output = self.assert_clean_image(source, root, "image/png")
            with Image.open(output) as cleaned:
                rgba = cleaned.convert("RGBA")
                self.assertEqual(rgba.getpixel((0, 0)), (255, 0, 0, 0))
                self.assertEqual(rgba.getpixel((1, 1)), (0, 255, 0, 255))
            truncated = root / "truncated.png"
            truncated.write_bytes(source.read_bytes()[:60])
            shutil.rmtree(root / "job")
            (root / "job").mkdir()
            with self.assertRaises(app.CleanError):
                app.clean_image(truncated, root / "job")

    def test_opaque_palette_png_passes_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "palette.png"
            palette = Image.new("P", (16, 12), 0)
            palette.putpalette([255, 0, 0, 0, 255, 0])
            palette.save(source)
            self.assert_clean_image(source, root, "image/png")

    def test_image_validator_still_flags_private_png_and_webp_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "tagged.png"
            Image.new("RGB", (16, 12), "#6a8c77").save(png)
            self.add_image_metadata(png)
            png_fields = app.exif_field_names(png, strict=True)
            self.assertIn("PNG:Comment", png_fields)
            self.assertIn("XMP-dc:Creator", png_fields)
            self.assertIn("IFD0:Artist", png_fields)
            self.assertNotIn("PNG:ImageWidth", png_fields)
            webp = root / "tagged.webp"
            Image.new("RGB", (16, 12), "#6a8c77").save(webp, lossless=True)
            self.add_image_metadata(webp)
            webp_fields = app.exif_field_names(webp, strict=True)
            self.assertIn("XMP-dc:Creator", webp_fields)
            self.assertIn("GPS:GPSLatitude", webp_fields)
            self.assertNotIn("RIFF:ImageWidth", webp_fields)
            # A structural field name in a group of another format is not exempt.
            self.assertFalse(app.is_image_derived("XMP-tiff", "ImageWidth"))

    def make_video(self, root: Path, suffix: str = ".mp4") -> Path:
        source = root / f"video{suffix}"
        subprocess.run(
            [
                "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0x6a8c77:s=96x64:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-metadata", "title=INERT_TITLE_SENTINEL",
                "-metadata", "comment=INERT_COMMENT_SENTINEL",
                "-metadata", "creation_time=2020-01-02T03:04:05Z",
                "-metadata", "location=+60.1699+024.9384/", str(source),
            ],
            check=True, capture_output=True,
        )
        return source

    def test_request_gate_local_and_tailnet(self):
        class Request:
            def __init__(self, host, method="GET", headers=None):
                self.method = method
                self.headers = {"host": host, **(headers or {})}
        self.assertTrue(app.authorize_request(Request("127.0.0.1:3495")))
        self.assertFalse(app.authorize_request(Request("evil.invalid")))
        self.assertFalse(app.authorize_request(Request("127.0.0.1:3495", headers={"x-forwarded-for": "1.2.3.4"})))
        forwarded = {"x-forwarded-for": "203.0.113.10", "x-forwarded-proto": "https", "tailscale-user-login": "owner"}
        self.assertTrue(app.authorize_request(Request(app.TAILNET_HOST, headers=forwarded)))
        self.assertFalse(app.authorize_request(Request(app.TAILNET_HOST, headers={**forwarded, "tailscale-user-login": "wrong"})))
        self.assertFalse(app.authorize_request(Request(app.TAILNET_HOST, headers={"tailscale-user-login": "owner"})))
        post = {**forwarded, "origin": f"https://{app.TAILNET_HOST}", "x-csrf-token": app.CSRF_TOKEN}
        self.assertTrue(app.authorize_request(Request(app.TAILNET_HOST, "POST", post)))
        self.assertFalse(app.authorize_request(Request(app.TAILNET_HOST, "POST", {**post, "origin": "https://evil.invalid"})))

    def test_jpeg_is_rebuilt_without_exif_gps_or_sentinels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_jpeg(root)
            job = root / "job"
            job.mkdir()
            output, media_type, report = app.clean_image(source, job)
            self.assertEqual(media_type, "image/jpeg")
            self.assertGreaterEqual(report["removed_fields"], 3)
            removed = {item["field"]: item["value"] for item in report["report"]["removed"]["fields"]}
            self.assertEqual(removed["IFD0:Artist"], "INERT_PERSON_SENTINEL")
            self.assertEqual(removed["File:Comment"], "INERT_COMMENT_SENTINEL")
            self.assertIn("GPS:GPSLatitude", removed)
            self.assertEqual(report["report"]["verification"]["remaining_count"], 0)
            self.assertEqual(app.exif_field_names(output), [])
            raw = output.read_bytes()
            self.assertNotIn(b"INERT_PERSON_SENTINEL", raw)
            self.assertNotIn(b"INERT_COMMENT_SENTINEL", raw)
            with Image.open(output) as cleaned:
                self.assertEqual(cleaned.size, (64, 48))
                self.assertEqual(len(cleaned.getexif()), 0)

    def test_video_drops_private_metadata_and_keeps_av(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_video(root)
            job = root / "job"
            job.mkdir()
            output, media_type, report = app.clean_video(source, job, ".mp4")
            self.assertEqual(media_type, "video/mp4")
            probe = app.probe_video(output)
            self.assertEqual(app.video_private_metadata(probe), [])
            self.assertEqual({s["codec_type"] for s in probe["streams"]}, {"video", "audio"})
            self.assertNotIn(b"INERT_TITLE_SENTINEL", output.read_bytes())
            self.assertNotIn(b"INERT_COMMENT_SENTINEL", output.read_bytes())
            self.assertEqual(report["kind"], "video")
            removed = {item["field"]: item["value"] for item in report["report"]["removed"]["fields"]}
            self.assertEqual(removed["säiliö:title"], "INERT_TITLE_SENTINEL")
            self.assertEqual(removed["säiliö:comment"], "INERT_COMMENT_SENTINEL")
            self.assertIn("säiliö:creation_time", removed)
            self.assertTrue(any("tyyppi=video" in item["value"] for item in report["report"]["kept_tracks"]["fields"]))
            self.assertTrue(any("tyyppi=audio" in item["value"] for item in report["report"]["kept_tracks"]["fields"]))
            self.assertEqual(report["report"]["verification"]["remaining_count"], 0)

    def test_video_drops_attached_cover_art(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_video(root)
            cover = root / "cover.jpg"
            Image.new("RGB", (48, 48), "#9c4639").save(cover, quality=90)
            with_cover = root / "video-with-cover.mp4"
            subprocess.run(
                [
                    "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-i", str(cover),
                    "-map", "0", "-map", "1", "-c", "copy",
                    "-disposition:v:1", "attached_pic", str(with_cover),
                ],
                check=True, capture_output=True,
            )
            self.assertTrue(
                any((stream.get("disposition") or {}).get("attached_pic") for stream in app.probe_video(with_cover)["streams"])
            )
            job = root / "job"
            job.mkdir()
            output, _, report = app.clean_video(with_cover, job, ".mp4")
            cleaned = app.probe_video(output)
            self.assertFalse(
                any((stream.get("disposition") or {}).get("attached_pic") for stream in cleaned["streams"])
            )
            self.assertEqual({stream["codec_type"] for stream in cleaned["streams"]}, {"video", "audio"})
            self.assertGreaterEqual(report["dropped_tracks"], 1)
            self.assertTrue(any("liitekuva=kyllä" in item["value"] for item in report["report"]["removed_tracks"]["fields"]))

    def test_report_values_are_bounded_and_control_characters_are_neutralized(self):
        value = "alku\x00<script>alert(1)</script>" + "x" * 500
        cleaned = app.report_value(value)
        self.assertNotIn("\x00", cleaned)
        self.assertIn("�", cleaned)
        self.assertLessEqual(len(cleaned), app.REPORT_VALUE_LIMIT + 1)
        self.assertTrue(cleaned.endswith("…"))

    def test_report_item_count_is_bounded_but_total_is_truthful(self):
        items = [{"field": f"field-{index:03d}", "value": "INERT"} for index in range(app.REPORT_MAX_ITEMS + 7)]
        report = app.bounded_report_items(items)
        self.assertEqual(len(report["fields"]), app.REPORT_MAX_ITEMS)
        self.assertEqual(report["field_count"], app.REPORT_MAX_ITEMS + 7)
        self.assertEqual(report["omitted_count"], 7)
        self.assertFalse(report["complete"])

    def test_unsupported_and_empty_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "document.pdf"
            source.write_bytes(b"%PDF-1.7\nINERT_SENTINEL")
            job = root / "job"
            job.mkdir()
            with self.assertRaises(app.CleanError):
                app.clean_file(source, job, ".pdf")

    def test_video_command_never_uses_original_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_video(root)
            job = root / "job"
            job.mkdir()
            with self.assertRaises(app.CleanError):
                app.clean_video(source, job, ".mp4;touch PWNED")
            self.assertFalse((root / "PWNED").exists())

    def test_video_parsers_allow_only_local_file_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_video(root)
            probe = app.probe_video(source)
            self.assertIn("streams", probe)
            job = root / "job"
            job.mkdir()
            with mock.patch.object(app.subprocess, "run", wraps=subprocess.run) as run:
                app.clean_video(source, job, ".mp4")
            commands = [call.args[0] for call in run.call_args_list]
            ffmpeg = next(command for command in commands if command[0] == "/usr/bin/ffmpeg")
            self.assertEqual(ffmpeg[ffmpeg.index("-protocol_whitelist") + 1], "file")

    def test_cleanup_expired_removes_only_expired_job_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_tmp = app.TMP_ROOT
            try:
                app.TMP_ROOT = root
                fresh = root / "fresh"
                stale = root / "stale"
                fresh.mkdir(); stale.mkdir()
                (fresh / "x").write_text("fresh")
                (stale / "x").write_text("stale")
                os.utime(stale, (1, 1))
                app.cleanup_expired_sync(now=app.JOB_TTL_SECONDS + 2)
                self.assertTrue(fresh.exists())
                self.assertFalse(stale.exists())
            finally:
                app.TMP_ROOT = old_tmp

    def test_upload_fails_closed_when_free_disk_reserve_is_not_met(self):
        class Request:
            async def stream(self):
                yield b"INERT"

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "input.bin"
            usage = shutil._ntuple_diskusage(total=100, used=90, free=10)
            with mock.patch.object(app.shutil, "disk_usage", return_value=usage):
                old_reserve = app.MIN_FREE_BYTES
                try:
                    app.MIN_FREE_BYTES = 20
                    with self.assertRaisesRegex(app.CleanError, "levytilaa"):
                        import asyncio
                        asyncio.run(app.save_upload(Request(), destination))
                finally:
                    app.MIN_FREE_BYTES = old_reserve
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
