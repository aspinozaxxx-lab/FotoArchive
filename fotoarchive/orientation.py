"""Orientation evidence uses faces and scene geometry, never camera date stamps."""
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import FACE_VERSION, QWEN_REV
from .media import open_rgb


ORIENTATION_VERSION = f"orientation-v1:{FACE_VERSION}:{QWEN_REV}:four-views:ignore-text"
ANGLES = (0, 90, 180, 270)


def rotate_clockwise(image, angle):
    operations = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    return image.transpose(operations[angle]) if angle else image.copy()


def comparison_image(image):
    canvas = Image.new("RGB", (1024, 1100), "#e8edef")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=30)
    for i, angle in enumerate(ANGLES):
        variant = rotate_clockwise(image, angle)
        variant.thumbnail((496, 492), Image.Resampling.LANCZOS)
        x, y = i % 2 * 512, i // 2 * 550
        draw.text((x+18, y+8), f"{chr(65+i)}", font=font, fill="#172a31")
        canvas.paste(variant, (x+(512-variant.width)//2, y+48+(492-variant.height)//2))
    stream = io.BytesIO()
    canvas.save(stream, format="JPEG", quality=92)
    return stream.getvalue()


def face_evidence(detector, image):
    image = image.copy()
    image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    evidence = []
    for angle in ANGLES:
        rgb = np.asarray(rotate_clockwise(image, angle))
        height, width = rgb.shape[:2]
        scores = []
        for box, points, confidence in detector.detect(rgb):
            eyes, mouth = points[:2].mean(0), points[3:].mean(0)
            down = mouth - eyes
            tilt = abs(math.degrees(math.atan2(float(down[0]), float(down[1]))))
            area = max(0., float(np.prod(box[2:]-box[:2]))) / (width * height)
            if tilt < 35 and min(box[2:]-box[:2]) >= 24:
                scores.append(confidence * math.sqrt(area))
        evidence.append({"rotation": angle, "faces": len(scores), "strength": round(sum(sorted(scores, reverse=True)[:6]), 5)})
    return evidence


class OrientationAnalyzer:
    def __init__(self, detector, vision):
        self.detector, self.vision = detector, vision

    def analyze(self, path):
        image = open_rgb(path)
        evidence = face_evidence(self.detector, image)
        ranked = sorted(evidence, key=lambda row: row["strength"], reverse=True)
        best, runner = ranked[:2]
        schema = {"type": "object", "properties": {
            "view": {"type": "string", "enum": ["A", "B", "C", "D", "uncertain"]},
            "basis": {"type": "string", "enum": ["people", "scene", "uncertain"]},
            "certain": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["view", "basis", "certain", "reason"], "additionalProperties": False}
        prompt = """Four labelled views A, B, C, D show the SAME photograph rotated in 90-degree steps.
Choose the view with the photograph's natural upright orientation. Use heads relative to bodies,
standing people, ground below feet, walls, trees, and horizon. People may lie down, lean, do sports or tilt their heads;
do not rotate an otherwise upright scene just to straighten one person's head.
IGNORE ALL TEXT WITHIN THE PHOTOGRAPHS, especially imprinted camera dates, captions and watermarks.
Their readability and direction are NOT evidence of the correct orientation. Only A/B/C/D outside the pictures identify views.
When several views are plausible, the scene lacks a reliable vertical, or people lie down without clear scene geometry,
return view uncertain and certain false. Use basis people, scene or uncertain. Reason: one short sentence
about visible physical evidence, never a date or readable text. Do not invent invisible body parts.
"""
        result = self.vision.complete(prompt, schema, max_tokens=240, image_data=comparison_image(image))
        view = result.get("view")
        rotation = ANGLES["ABCD".index(view)] if view in ("A", "B", "C", "D") else None
        certain = result.get("certain") is True and result.get("basis") in {"people", "scene"} and rotation is not None
        # Conflicting independent face evidence is deliberately left for review.
        if rotation is not None and best["strength"] >= .06 and best["strength"] > runner["strength"] * 1.8 and rotation != best["rotation"]:
            certain = False
        if rotation is None and best["rotation"] and best["strength"] >= .025:
            rotation = best["rotation"]
        reason = {'people':'Предложение модели по положению людей относительно тела, земли и окружения.',
                  'scene':'Предложение модели по вертикалям и геометрии сцены.',
                  'uncertain':'Уверенно определить естественный верх сцены не удалось.'}.get(result.get('basis'),'Требуется ручная проверка.')
        if not certain:
            reason += ' Проверьте направление вручную: свидетельства неоднозначны.'
        return {"rotation": rotation, "certain": certain, "reason": reason, "model_evidence": str(result.get('reason',''))[:800],
                "basis": result.get("basis", "uncertain"), "faces": evidence}
