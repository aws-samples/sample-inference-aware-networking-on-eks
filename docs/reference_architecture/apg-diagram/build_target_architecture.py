#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The APG pattern's target architecture diagram, as an editable PowerPoint slide.

    ./.venv/bin/python docs/reference_architecture/apg-diagram/build_target_architecture.py

WHY POWERPOINT IS THE SOURCE. The APG Bar Raiser asked for the diagram "using current AWS
Architecture Icons", with "the S1-S8 and A1-A7 callouts the text references" labeled, and the
"PPT/Visio source" attached (review of 2026-10-30, Q3). So the .pptx is the source of truth and
the PNG uploaded to APG is exported from it, never drawn separately.

WHAT FOLLOWS THE AWS ARCHITECTURE ICONS DECK, release 2026.07.31, read from the deck itself:
group boxes with 1.25 pt lines in the deck's colors (AWS Cloud black, Region teal dashed, VPC
purple, a custom Amazon EKS group in the Containers category orange), group icons at 0.417 in,
service and resource icons at 0.5 in, labels in 12 pt Arial, arrows as 1.25 pt black lines with
the open arrowhead and right angles, and numbered callouts as 0.36 in black ovals with bold
14 pt white Arial. One deliberate departure: the deck says callouts carry numbers only, and the
Bar Raiser asked for S and A labels, so the callouts read S1 to S8 and A1 to A7.

THE ICONS ARE VENDORED UNALTERED in icons/, copied from the AWS Architecture Icons package
Icon-package_07312026 (zip sha256 d2d166c453526471...c1992dc6526, the same package the blog figure
uses). The public sample ships the diagram but not the icon files, so to rebuild it there, download
that package from aws.amazon.com/architecture/icons and copy the 13 files named in ICON below into
icons/. Each icon is embedded as SVG with a PNG fallback, which is how the AWS deck stores its own
icons, so the slide stays sharp at any zoom. The PNG fallbacks in icons/png/ are rendered from
those SVGs by headless Chrome.

WHAT THE DIAGRAM MUST NOT CLAIM, re-read from the code for this drawing:
  - The relay forwards every admitted request to the gateway, batch-labeled ones included, and
    enqueues on Amazon SQS only when it promotes (async-worker/relay_server.py:13 and :148).
  - Queued jobs enter through POST /submit on the submit API, a ClusterIP Service, so job
    producers sit inside the cluster (async-worker/submit_api.py:1-16).
  - The submit API writes no DynamoDB item; the worker's conditional claim creates the record.
  - The walkthrough does not apply the relay, its Network Load Balancer, or the premium and
    economy pools. Those are dashed, and the legend says so.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "icons")
PNGS = os.path.join(ICONS, "png")
OUT = os.path.join(HERE, "target-architecture.pptx")
OUT_PNG = os.path.join(HERE, "target-architecture.png")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

FONT = "Arial"
BLACK = RGBColor(0x00, 0x00, 0x00)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
MUTED = RGBColor(0x54, 0x5B, 0x64)
GRAY = RGBColor(0x7D, 0x89, 0x98)
TEAL = RGBColor(0x00, 0xA4, 0xA6)
PURPLE = RGBColor(0x8C, 0x4F, 0xFF)
ORANGE = RGBColor(0xED, 0x71, 0x00)
GROUP_W = Emu(15875)          # 1.25 pt, the deck's group and arrow weight
BOX_W = Pt(1)
SVG_EXT = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"

ICON = {
    "eks": "Arch_Amazon-Elastic-Kubernetes-Service_32",
    "sqs": "Arch_Amazon-Simple-Queue-Service_48",
    "ddb": "Arch_Amazon-DynamoDB_48",
    "ecr": "Arch_Amazon-Elastic-Container-Registry_48",
    "cw": "Arch_Amazon-CloudWatch_48",
    "iam": "Arch_AWS-Identity-and-Access-Management_48",
    "nlb": "Res_Elastic-Load-Balancing_Network-Load-Balancer_48",
    "ec2": "Res_Amazon-EC2_Instance_48",
    "users": "Res_Users_48_Light",
    "app": "Res_Generic-Application_48_Light",
    "cloud": "AWS-Cloud-logo_32",
    "region": "Region_32",
    "vpc": "Virtual-private-cloud-VPC_32",
}


def rasterize() -> None:
    """PNG fallbacks for the SVG icons, rendered by headless Chrome with a transparent background."""
    os.makedirs(PNGS, exist_ok=True)
    tmp = os.path.join(PNGS, "_page.html")
    for name in ICON.values():
        out = os.path.join(PNGS, name + ".png")
        if os.path.exists(out):
            continue
        svg = os.path.join(ICONS, name + ".svg")
        with open(tmp, "w") as f:
            f.write('<html><body style="margin:0;background:transparent">'
                    '<img src="file://%s" width="256" height="256"></body></html>' % svg)
        # Maintainer tool: fixed local renderer argv, list form, no shell.
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",  # nosec B603
                        "--default-background-color=00000000", "--window-size=256,256",
                        "--screenshot=" + out, "file://" + tmp],
                       check=True, capture_output=True)
    if os.path.exists(tmp):
        os.remove(tmp)


prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
slide = prs.slides.add_slide(prs.slide_layouts[6])
SP = slide.shapes
_svg_n = 0


def _ln(shape):
    return shape.line._get_or_add_ln()


def line_style(shape, color=BLACK, width=GROUP_W, dash=None, head=False, tail=True):
    shape.line.color.rgb = color
    shape.line.width = width
    ln = _ln(shape)
    if dash:
        d = etree.SubElement(ln, qn("a:prstDash"))
        d.set("val", dash)
    for tag, on in (("a:headEnd", head), ("a:tailEnd", tail)):
        e = etree.SubElement(ln, qn(tag))
        e.set("type", "arrow" if on else "none")
        e.set("w", "med")
        e.set("len", "sm")


def text(x, y, w, h, lines, size=12, bold_first=False, align=PP_ALIGN.CENTER, color=BLACK,
         anchor=MSO_ANCHOR.TOP, name=None):
    tb = SP.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = ln
        r.font.name, r.font.size, r.font.color.rgb = FONT, Pt(size), color
        r.font.bold = bold_first and i == 0
    if name:
        tb.name = name
    return tb


def icon(key, x, y, size=0.5, name=None):
    global _svg_n
    png = os.path.join(PNGS, ICON[key] + ".png")
    pic = SP.add_picture(png, Inches(x), Inches(y), Inches(size), Inches(size))
    pic.name = name or ICON[key]
    with open(os.path.join(ICONS, ICON[key] + ".svg"), "rb") as f:
        data = f.read()
    _svg_n += 1
    part = Part(PackURI("/ppt/media/aws-icon-%02d.svg" % _svg_n), "image/svg+xml", slide.part.package, data)
    rid = slide.part.relate_to(part, RT.IMAGE)
    blip = pic._element.find(".//" + qn("a:blip"))
    ext_lst = etree.SubElement(blip, qn("a:extLst"))
    ext = etree.SubElement(ext_lst, qn("a:ext"))
    ext.set("uri", SVG_EXT)
    svgb = etree.SubElement(ext, "{%s}svgBlip" % ASVG, nsmap={"asvg": ASVG})
    svgb.set(qn("r:embed"), rid)
    return pic


def group(x, y, w, h, label, color, icon_key=None, dash=None, name=None):
    r = SP.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    r.fill.background()
    line_style(r, color=color, dash=dash, tail=False)
    r.shadow.inherit = False
    r.name = name or label
    r.text_frame.text = ""
    if icon_key:
        icon(icon_key, x, y, 0.417, name=label + " icon")
    text(x + 0.48, y + 0.1, w - 0.6, 0.25, [label], align=PP_ALIGN.LEFT, name=label + " label")


def box(x, y, w, h, title, sub=None, dashed=False, name=None):
    r = SP.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    r.fill.solid()
    r.fill.fore_color.rgb = WHITE
    line_style(r, color=GRAY, width=BOX_W, dash="dash" if dashed else None, tail=False)
    r.shadow.inherit = False
    r.name = name or title
    tf = r.text_frame
    tf.margin_left = tf.margin_right = Inches(0.04)
    tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.word_wrap = True
    for i, ln in enumerate([title] + ([sub] if sub else [])):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = ln
        run.font.name, run.font.size, run.font.bold = FONT, Pt(12), i == 0
        run.font.color.rgb = BLACK
    return r


def pool(x, y, w, h, title, sub, dashed=False):
    box(x, y, w, h, "", None, dashed=dashed, name=title + " box")
    icon("ec2", x + 0.08, y + (h - 0.36) / 2, 0.36, name=title + " node icon")
    text(x + 0.52, y + 0.03, w - 0.58, h - 0.06, [title, sub], bold_first=True, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.MIDDLE, name=title)


def arrow(points, dashed=False, head=False, name=None):
    """A right-angled arrow through the given points, in inches; the arrowhead is on the last point."""
    segs = []
    last = len(points) - 2
    for i, ((x1, y1), (x2, y2)) in enumerate(zip(points, points[1:])):
        c = SP.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        line_style(c, dash="dash" if dashed else None, head=head and i == 0, tail=i == last)
        _ln(c).set("cap", "sq")      # square caps close the joint between segments
        segs.append(c)
    if len(segs) == 1:
        if name:
            segs[0].name = name
        return segs[0]
    grp = SP.add_group_shape(segs)
    if name:
        grp.name = name
    return grp


def callout(label, x, y):
    o = SP.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Emu(329184), Emu(329184))
    o.fill.solid()
    o.fill.fore_color.rgb = BLACK
    o.line.fill.background()
    o.shadow.inherit = False
    o.name = "Callout " + label
    tf = o.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.name, r.font.size, r.font.bold, r.font.color.rgb = FONT, Pt(14), True, WHITE


def labeled_icon(key, cx, y, lines, name=None, width=1.6):
    icon(key, cx - 0.25, y, 0.5, name=name)
    if lines:
        text(cx - width / 2, y + 0.54, width, 0.42, lines, name=(name or lines[0]) + " label")


def build() -> None:
    # Groups, outermost first so inner content draws on top.
    group(0.20, 0.15, 12.93, 6.87, "AWS Cloud", BLACK, "cloud")
    group(0.35, 0.65, 12.63, 6.27, "AWS Region", TEAL, "region", dash="sysDash")
    group(0.50, 1.15, 12.33, 4.47, "VPC", PURPLE, "vpc")
    group(2.85, 1.65, 9.83, 3.80, "Amazon EKS cluster", ORANGE, "eks")

    # Entry, outside the cluster but inside the VPC: the load balancer is internal.
    labeled_icon("users", 1.00, 2.25, ["Clients", "in the VPC"], name="Clients", width=1.0)
    labeled_icon("nlb", 2.10, 2.25, ["Network Load", "Balancer (internal)"], name="Network Load Balancer", width=1.3)

    # Synchronous row.
    box(3.05, 2.25, 1.80, 0.60, "Relay (2 replicas)", "labels, limits", dashed=True)
    box(5.30, 2.25, 1.85, 0.60, "Envoy Gateway", "+ Agent Router")
    box(9.60, 2.25, 2.95, 0.60, "Endpoint Picker", "one per InferencePool")
    pools = [("Interactive pool", "Qwen3-8B on G6e", False),
             ("Premium pool", "Qwen3-32B-AWQ on G6e", True),
             ("Batch pool", "Qwen3-8B on G6", False),
             ("Economy pool", "Qwen3-1.7B on G6", True)]
    mids = []
    for i, (t, s, d) in enumerate(pools):
        y = 3.10 + i * 0.54
        pool(9.60, y, 2.95, 0.46, t, s, dashed=d)
        mids.append(y + 0.23)

    # Asynchronous row and the producers above it.
    labeled_icon("app", 4.85, 3.05, [], name="Job producers")
    text(5.18, 3.12, 1.20, 0.40, ["Job producers"], align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE,
         name="Job producers text")
    box(4.15, 4.15, 1.40, 0.60, "Submit API", "POST /submit")
    box(5.95, 4.15, 1.50, 0.60, "Async worker", "claims each job")
    box(7.70, 4.15, 1.50, 0.60, "Orphan sweeper", "every minute")
    text(5.40, 1.75, 7.15, 0.25, ["Add-ons: Karpenter, KEDA, kube-prometheus-stack, DCGM exporter, cost safety net"],
         align=PP_ALIGN.RIGHT, color=MUTED, name="Cluster add-ons")

    # Regional services below the VPC.
    icon("sqs", 4.60, 5.82, 0.5, name="Amazon SQS")
    text(5.18, 5.84, 1.30, 0.46, ["Amazon SQS", "queue and DLQ"], align=PP_ALIGN.LEFT, name="Amazon SQS label")
    labeled_icon("ddb", 7.95, 5.82, ["Amazon DynamoDB", "inference-jobs table"], name="Amazon DynamoDB", width=1.6)
    labeled_icon("ecr", 10.05, 5.82, ["Amazon ECR"], name="Amazon ECR", width=1.1)
    labeled_icon("cw", 11.25, 5.82, ["Amazon", "CloudWatch"], name="Amazon CloudWatch", width=1.1)
    labeled_icon("iam", 12.40, 5.82, ["AWS IAM"], name="AWS IAM", width=0.9)

    # Arrows. Dashed means the walkthrough does not apply it.
    arrow([(1.25, 2.50), (1.85, 2.50)], dashed=True, name="S1 client to load balancer")
    arrow([(2.35, 2.50), (3.05, 2.50)], dashed=True, name="S1 load balancer to relay")
    arrow([(4.85, 2.55), (5.30, 2.55)], dashed=True, name="S4 relay to gateway")
    arrow([(7.15, 2.40), (9.60, 2.40)], name="S5 gateway to Endpoint Picker")
    arrow([(11.05, 2.85), (11.05, 3.10)], head=True, name="S6 Endpoint Picker and pool metrics")
    arrow([(7.15, 2.70), (9.35, 2.70), (9.35, mids[0]), (9.60, mids[0])], name="S7 to interactive pool")
    arrow([(9.35, mids[0]), (9.35, mids[2]), (9.60, mids[2])], name="S7 to batch pool")
    arrow([(9.35, mids[1]), (9.60, mids[1])], dashed=True, name="S7 to premium pool")
    arrow([(9.35, mids[2]), (9.35, mids[3]), (9.60, mids[3])], dashed=True, name="S7 to economy pool")
    arrow([(3.12, 2.85), (3.12, 6.07), (4.60, 6.07)], dashed=True, name="S8 promotion to Amazon SQS")
    arrow([(4.75, 3.55), (4.75, 4.15)], name="A1 producers to submit API")
    arrow([(5.00, 4.15), (5.00, 3.55)], name="A6 result to producers")
    arrow([(4.68, 4.75), (4.68, 5.82)], name="A1 submit API enqueues")
    arrow([(5.02, 5.82), (5.02, 5.12), (6.20, 5.12), (6.20, 4.75)], name="A2 receive")
    arrow([(7.20, 4.75), (7.20, 6.07), (7.70, 6.07)], name="A2 and A5 claim and result")
    arrow([(6.95, 4.15), (6.95, 2.85)], name="A4 worker to gateway")
    arrow([(7.95, 4.75), (7.95, 5.82)], name="A7 sweeper scans the table")
    arrow([(9.05, 4.75), (9.05, 6.84), (4.85, 6.84), (4.85, 6.32)], name="A7 sweeper re-enqueues")

    # Callouts.
    for lab, x, y in [("S1", 1.37, 1.98), ("S2", 3.58, 2.90), ("S3", 4.02, 2.90), ("S4", 4.90, 2.62),
                      ("S5", 8.20, 2.01), ("S6", 11.20, 2.80), ("S7", 8.20, 2.76), ("S8", 3.22, 4.95),
                      ("A1", 4.33, 3.67), ("A2", 5.58, 4.76), ("A3", 5.78, 3.97), ("A4", 7.02, 3.30),
                      ("A5", 7.25, 5.20), ("A6", 5.08, 3.67), ("A7", 8.32, 5.00)]:
        callout(lab, x, y)

    # Legend, below the AWS Cloud group.
    arrow([(0.35, 7.25), (0.95, 7.25)], name="Legend solid")
    text(1.05, 7.15, 2.6, 0.22, ["Deployed by the walkthrough"], align=PP_ALIGN.LEFT, name="Legend solid text")
    arrow([(3.75, 7.25), (4.35, 7.25)], dashed=True, name="Legend dashed")
    text(4.45, 7.15, 4.2, 0.22, ["Committed and tested, not applied by the walkthrough"], align=PP_ALIGN.LEFT,
         name="Legend dashed text")
    text(8.85, 7.15, 4.3, 0.22, ["S: synchronous flow    A: asynchronous flow"], align=PP_ALIGN.LEFT,
         name="Legend callouts text")


def render_png() -> None:
    """The APG image, rendered FROM the saved .pptx by macOS Quick Look, so it cannot drift from the source."""
    tmp = os.path.join(HERE, "_render")
    os.makedirs(tmp, exist_ok=True)
    # Maintainer tool: fixed local renderer argv, list form, no shell.
    subprocess.run(["qlmanage", "-t", "-s", "2600", "-o", tmp, OUT], check=True, capture_output=True)  # nosec B603
    shutil.move(os.path.join(tmp, os.path.basename(OUT) + ".png"), OUT_PNG)
    os.rmdir(tmp)


if __name__ == "__main__":
    rasterize()
    build()
    prs.save(OUT)
    render_png()
    print("wrote", OUT, "with", len(slide.shapes), "shapes and", _svg_n, "SVG icons, and", OUT_PNG)
