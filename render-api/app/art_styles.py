"""Sextant-owned visual style catalog for Bible media generation."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


_CATALOG = """
Baroque|Sacred & historical|Dramatic light contrasts (chiaroscuro), emotional intensity, dynamic compositions
Renaissance|Sacred & historical|Symmetry, harmony, idealized human forms, architectural details, linear perspective and spatial realism
Northern Renaissance|Sacred & historical|Detailed natural landscapes, moral allegory and rich symbolism, rich oil-painted textures and vibrant colors
Dutch Golden Age|Sacred & historical|Detailed domestic scenes, warm natural lighting, focus on everyday life
Pre-Raphaelite|Sacred & historical|Intricate botanical details, vibrant colors, emphasis on beauty and purity
Romanticism|Sacred & historical|Emotion-driven imagery, dramatic landscapes, contrasts between light and shadow
Neoclassical|Sacred & historical|Clean lines, symmetrical composition, emphasis on moral clarity and virtue
Venetian Renaissance|Sacred & historical|Vibrant atmospheric light, harmonious compositions, natural beauty blended with divine themes
Italian Renaissance|Sacred & historical|Idealized human forms, balanced compositions, serene divine symmetry and harmony
Gothic|Sacred & historical|Intricate architectural details, spiritual awe, dramatic settings
Symbolism|Sacred & historical|Surreal and allegorical elements, bold contrasts, symbolic representations
Byzantine Iconography|Sacred & historical|Flat symbolic figures, gold-leaf backgrounds, intricate geometric patterns
Romanesque Relief|Sacred & historical|Bold simplified lines, earthy tones, stone carvings and murals
Gothic Stained Glass|Sacred & historical|Jewel-toned colors, intricate details, radiant light effects
Medieval Illuminated Manuscript|Sacred & historical|Vibrant colors, intricate floral or vine patterns, decorative borders
Impressionism|Sacred & historical|Soft brushstrokes, dynamic lighting, emotional warmth and communal joy
High Renaissance|Sacred & historical|Majestic idealized figures, grandeur of divine moments, unified narrative composition
Romantic Allegory|Sacred & historical|Sweeping landscapes, atmospheric lighting, symbolic representations
Romantic Sublime|Sacred & historical|Awe and grandeur of nature, spiritual undertones, dramatic contrasts
Arts and Crafts Movement|Sacred & historical|Symmetry, natural motifs, handcrafted quality
3D model|General art & illustration|Dimensional modeling, polished rendering, sculptural depth
Anime|General art & illustration|Expressive character design, stylized line work, vivid colors and shading
Cinematic|Sacred & historical|Realistic lighting, dramatic composition, photorealism with restrained cinematic stylization
Comic book|General art & illustration|Bold line work, panel-inspired storytelling, high-contrast colors
Clay|General art & illustration|Tactile handmade aesthetic, stop-motion character, soft organic forms
Digital art|General art & illustration|Layered digital painting, versatile mixed media, polished contemporary finish
Enhance|General art & illustration|Hyper-realistic detailing, refined post-processing, exceptional visual clarity
Fantasy art|General art & illustration|Mythical themes, heightened atmosphere, detailed world-building
Illustration|General art & illustration|Hand-drawn quality, narrative emphasis, adaptable editorial composition
Line art|General art & illustration|Minimalist design, monochromatic or limited color, expressive flowing lines
Low poly|General art & illustration|Geometric simplicity, flat shading, stylized dimensional forms
Neon punk|Genre & speculative|Vibrant neon colors, cyberpunk aesthetic, high-contrast glow effects
Origami|General art & illustration|Folded-paper appearance, minimalist complexity, strong silhouettes
Photographic|Nature & documentary|High realism, dynamic photographic composition, refined post-processing
Futuristic|Genre & speculative|Advanced technology aesthetic, precise forms, bright metallic colors
Cyber|Genre & speculative|Digital influence, virtual-reality aesthetic, monochrome and neon palette
Cyberpunk|Genre & speculative|Dystopian futurism, neon high contrast, urban decay fused with technology
Steampunk|Genre & speculative|Victorian aesthetic, brass gears and steam, intricate retro-futurism
Dieselpunk|Genre & speculative|Industrial military aesthetic, dark gritty tones, alternate-history machinery
80s Retro-Futurism|Genre & speculative|Optimistic sleek design, grand architecture, neon and metallic palette, advanced vehicles
Wasteland Punk|Genre & speculative|Improvised technology, desolate landscapes, brutalist forms, rust and earthy colors
Skybound Adventurism|Genre & speculative|Golden-age aviation, romantic adventure, whimsical diesel-era technology, warm vibrant color
Naturalist Realism|Nature & documentary|Highly detailed realism, authentic natural lighting, true-to-life color, environmental integration
Wildlife Storybook Realism|Nature & documentary|Softened realism, warm vibrant colors, playful composition, gently illustrated backgrounds
Victorian Expedition Sketchwork|Nature & documentary|Hand-drawn naturalist illustration, sepia earth tones, annotations, aged paper texture
Frontier Documentary Photography|Nature & documentary|Glass-plate photographic detail, soft long-exposure focus, vast landscapes, aged historical character
Modern Infographic Realism|Commercial & editorial|Clean 2D infographics, subtle 3D depth, disciplined corporate color palette
Flat Vector Animation|General art & illustration|Minimalist cartoon aesthetic, clean characters, crisp color blocks and graphic forms
Hand-Drawn Whiteboard Explainer|Commercial & editorial|Sketchy black-and-white drawing, restrained color, casual approachable style
Luxury Cinematic 3D Rendering|Commercial & editorial|Photorealistic 3D environments, cinematic lighting, polished luxury color palette
Retro Blueprint & Architectural Sketch|Commercial & editorial|Blueprint aesthetic, hand-sketched details, precise architectural line work
Neon Tech HUD (Heads-Up Display)|Genre & speculative|Futuristic interface design, high-tech color palette, luminous layered graphics
Vintage 1950s Real Estate Cartoon|Commercial & editorial|Mid-century cartoon style, muted pastel palette, narrative advertising composition
Photorealistic Nature & Property Documentary|Nature & documentary|High-definition landscape realism, golden-hour lighting, documentary framing
Minimalist Scandinavian Design|Commercial & editorial|Soft airy palette, simplified geometry, smooth minimalist dimensional rendering
Comic Book Real Estate Stories|Commercial & editorial|Bold inked line art, panel-based layout, energetic color contrast
3D Isometric City Builder|Commercial & editorial|Low-poly isometric design, miniature city forms, color-coded architectural detail
AI-Generated Dreamlike Homes|Commercial & editorial|Surreal architectural concepts, soft glowing textures, ethereal dreamlike atmosphere
High-Gloss Luxury Ad|Commercial & editorial|Cinematic camera language, rich color grading, pristine luxury finish
Fast-Paced Urban Lifestyle Ad|Commercial & editorial|Dynamic urban composition, vibrant city imagery, energetic commercial polish
Heartwarming Family Home Ad|Commercial & editorial|Soft natural lighting, emotional family storytelling, welcoming domestic atmosphere
Sleek Corporate Commercial|Commercial & editorial|Modern minimalist design, high-tech graphics, serious professional finish
Dream Home Fairytale Ad|Commercial & editorial|Soft-focus cinematic look, poetic fairytale atmosphere, sweeping architectural views
Comedy-Driven Real Estate Ad|Commercial & editorial|Playful exaggerated humor, bright bold colors, expressive advertising composition
Retro 80s/90s Infomercial Style|Commercial & editorial|Low-fi VHS effects, exuberant sales imagery, colorful retro graphics
High-Energy Sports-Style Ad|Commercial & editorial|Action-focused composition, athletic competitive tone, dynamic graphic energy
Real Estate Documentary Commercial|Commercial & editorial|Authentic documentary framing, personal human stories, understated natural imagery
Animated 3D Explainer Ad|Commercial & editorial|Sleek high-tech 3D animation, virtual walkthrough styling, polished explanatory graphics
Modern Real Estate Service Commercial|Commercial & editorial|Clean professional imagery, smooth cinematic composition, minimalist motion-graphic language
Professional Real Estate Photography Services|Commercial & editorial|Bright balanced exposure, accurate color, corrected wide-angle perspective, high-resolution detail
Mortgage Brand Hero|Commercial & editorial|Dynamic perspective framing, bold accent lighting, confident brand-centered focus
Mortgage Investor Spotlight|Commercial & editorial|Neighborhood-scale storytelling, portfolio growth imagery, warm partnership lighting
Mortgage Family Haven|Commercial & editorial|Front-porch welcome, everyday family life, soft neighborhood palette
Cinematic natural light|Sacred & historical|Cinematic natural light, grounded historical realism, coherent lighting
Classical oil painting|Sacred & historical|Classical oil painting, layered pigments, museum-quality composition
Fortress Grid illustration|General art & illustration|Flat Fortress Grid illustration, clean geometric forms, restrained palette
Historical documentary|Sacred & historical|Historical documentary realism, authentic material culture, natural available light
""".strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@lru_cache(maxsize=1)
def list_art_styles() -> list[dict[str, str]]:
    styles = []
    for line in _CATALOG.splitlines():
        name, category, prompt = (part.strip() for part in line.split("|", 2))
        styles.append({"id": _slug(name), "name": name, "category": category, "prompt": prompt})
    return styles


def resolve_art_style(value: str) -> dict[str, Any]:
    normalized = value.strip().lower()
    for style in list_art_styles():
        if normalized in {style["id"].lower(), style["name"].lower()}:
            return dict(style)
    cleaned = value.strip()
    return {
        "id": _slug(cleaned),
        "name": cleaned,
        "category": "Custom",
        "prompt": cleaned,
    }
