#!/usr/bin/env python3

import os
import sys
import json
import shutil
import tempfile
import zipfile
import configparser
from pathlib import Path
import re

# ---------------------------
# Config & helpers
# ---------------------------

def load_config():
    """Load configuration from config.ini and return a dict of settings."""
    cfg = configparser.ConfigParser()
    cfg.read('config.ini')

    DEFAULT = cfg['DEFAULT'] if 'DEFAULT' in cfg else {}

    # Load string options
    generated_folder_name = DEFAULT.get("generated_folder_name", "generated-resources")
    patched_prefix = DEFAULT.get("patched_prefix", "Patched ")
    generated_name_default = DEFAULT.get("generated_name_default", "generated-resources")

    # Load boolean options
    verbose = DEFAULT.get("verbose", "true").lower() in ("1", "true", "yes")
    prompt_for_generated_name = DEFAULT.get("prompt_for_generated_name", "true").lower() in ("1", "true", "yes")

    return {
        "generated_folder_name": generated_folder_name,
        "patched_prefix": patched_prefix,
        "generated_name_default": generated_name_default,
        "verbose": verbose,
        "prompt_for_generated_name": prompt_for_generated_name
    }

# Ensure directory exists
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

# Copy all root-level files and directories except "assets"
def copy_root_files(src_root, dest_root):
    for item in os.listdir(src_root):
        if item.lower() == "assets":
            continue
        src_item = os.path.join(src_root, item)
        dest_item = os.path.join(dest_root, item)
        if os.path.isdir(src_item):
            if os.path.exists(dest_item):
                log(f"Skipping existing directory {dest_item}")
            else:
                try:
                    shutil.copytree(src_item, dest_item)
                    log(f"Copied directory {item} -> {dest_item}")
                except Exception as e:
                    log(f"Failed copying directory {item}: {e}")
        else:
            if os.path.exists(dest_item):
                log(f"Skipping existing file {dest_item}")
            else:
                shutil.copy2(src_item, dest_item)
                log(f"Copied file {item} -> {dest_item}")

# Simple .properties parser
def parse_properties(file_path):
    data = {}
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data

# Transform filename -> case "when" value to reflect vanilla style
def transform_name_to_vanilla(filename_no_ext):
    parts = filename_no_ext.split('_')
    idx = None
    for i, p in enumerate(parts):
        if any(ch.isdigit() for ch in p):
            idx = i
            break
    if idx is None:
        # No numeric segment: capitalize and join with spaces
        head = ' '.join(p.capitalize() for p in parts if p != '')
        return head
    else:
        head_parts = parts[:idx]
        tail_parts = parts[idx:]
        head = ' '.join(p.capitalize() for p in head_parts if p != '')
        tail = '_'.join(tail_parts)
        return f"{head}_{tail}" if head else tail

# Load block names from minecraft_blocks.txt to check block vs item fallbacks
def load_block_names():
    block_file = "minecraft_blocks.txt"
    names = set()
    if os.path.exists(block_file):
        with open(block_file, "r", encoding="utf-8") as f:
            for line in f:
                n = line.strip()
                if n and not n.startswith("#"):
                    names.add(n)
        log(f"Loaded {len(names)} block names from {block_file}")
    else:
        log(f"Warning: {block_file} not found; defaulting to items-only fallback")
    return names

# JSON helpers
def safe_load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return None

# Write JSON with pretty formatting
def write_json_pretty(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf-8')

# Prints only if verbose is enabled in config (set by main)
def log(msg):
    if GLOBAL_CONFIG.get("verbose", True):
        print(msg)

# ---------------------------
# Item JSON merging (selector)
# ---------------------------

# Merge or create item JSON with select model for custom_name component
def merge_item_json(item_json_path, case_when, case_model_path, fallback_model):
    existing = safe_load_json(item_json_path)
    if existing and isinstance(existing, dict):
        try:
            model_block = existing.setdefault("model", {})
            model_block.setdefault("type", "minecraft:select")
            model_block.setdefault("property", "minecraft:component")
            model_block.setdefault("component", "minecraft:custom_name")
            cases = model_block.setdefault("cases", [])
            if any(c.get("when") == case_when for c in cases):
                log(f"Case '{case_when}' already present in {item_json_path}; skipping append")
            else:
                cases.append({
                    "when": case_when,
                    "model": {
                        "type": "minecraft:model",
                        "model": case_model_path
                    }
                })
            if isinstance(fallback_model, dict):
                # Directly use complex/special fallback definition
                model_block.setdefault("fallback", fallback_model)
            else:
                # Normal string fallback path
                model_block.setdefault("fallback", {
                    "type": "minecraft:model",
                    "model": fallback_model,
                    "tints": []
                })
            write_json_pretty(item_json_path, existing)
            return
        except Exception as e:
            log(f"Could not merge into existing {item_json_path}: {e}")

    # create new structure if we couldn't merge
    if isinstance(fallback_model, dict):
        fallback_block = fallback_model
    else:
        fallback_block = {
            "type": "minecraft:model",
            "model": fallback_model,
            "tints": []
        }

    new_obj = {
        "model": {
            "type": "minecraft:select",
            "property": "minecraft:component",
            "component": "minecraft:custom_name",
            "cases": [
                {
                    "when": case_when,
                    "model": {
                        "type": "minecraft:model",
                        "model": case_model_path
                    }
                }
            ],
            "fallback": fallback_block
        }
    }
    write_json_pretty(item_json_path, new_obj)

def item_model_atlas(model_definition):
    """Return the atlas used by a vanilla fallback item-model definition."""
    if isinstance(model_definition, str):
        path = model_definition.split(":", 1)[-1]
        return "block" if path.startswith("block/") else "item"
    if isinstance(model_definition, dict):
        for key in ("model", "base"):
            value = model_definition.get(key)
            if isinstance(value, str):
                path = value.split(":", 1)[-1]
                if path.startswith("block/"):
                    return "block"
        for value in model_definition.values():
            if isinstance(value, (dict, list)) and item_model_atlas(value) == "block":
                return "block"
    elif isinstance(model_definition, list):
        for value in model_definition:
            if item_model_atlas(value) == "block":
                return "block"
    return "item"

def normalise_item_case_atlases(dest_items_path, generated_asset_root, generated_folder_name):
    """Keep every case in an item selector on the same atlas as its fallback."""
    model_targets = {}
    atlas_sources = {"item": set(), "block": set()}
    for item_json_path in Path(dest_items_path).glob("*.json"):
        data = safe_load_json(item_json_path)
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, dict):
            continue

        target_atlas = item_model_atlas(model.get("fallback"))
        for case in model.get("cases", []):
            case_model = case.get("model") if isinstance(case, dict) else None
            model_path = case_model.get("model") if isinstance(case_model, dict) else None
            prefix = f"{generated_folder_name}:item/"
            if not isinstance(model_path, str) or not model_path.startswith(prefix):
                continue
            model_name = model_path[len(prefix):]
            model_targets.setdefault(model_name, set()).add(target_atlas)

    for model_name, target_atlases in model_targets.items():
        if len(target_atlases) != 1:
            log(f"Model {model_name} is shared by item definitions using different atlases; leaving it unchanged")
            continue

        target_atlas = next(iter(target_atlases))
        model_path = Path(generated_asset_root) / "models" / "item" / f"{model_name}.json"
        data = safe_load_json(model_path)
        textures = data.get("textures") if isinstance(data, dict) else None
        if not isinstance(textures, dict):
            continue

        changed = False
        for key, value in list(textures.items()):
            if not isinstance(value, str) or ":" not in value:
                continue
            namespace, path = value.split(":", 1)
            parts = path.split("/", 1)
            if len(parts) != 2:
                continue
            atlas, texture_name = parts
            if namespace == generated_folder_name and atlas in TEXTURE_ATLAS_ROOTS and atlas != target_atlas:
                textures[key] = f"{namespace}:{target_atlas}/{texture_name}"
                changed = True
            elif namespace == "minecraft" and atlas in TEXTURE_ATLAS_ROOTS and atlas != target_atlas:
                sprite = f"{generated_folder_name}:{target_atlas}/cit_patcher/{atlas}/{texture_name}"
                atlas_sources[target_atlas].add((value, sprite))
                textures[key] = sprite
                changed = True

        if changed:
            write_json_pretty(model_path, data)
            log(f"Normalised {model_name} textures to the {target_atlas} atlas")

    assets_dir = Path(dest_items_path).parents[1]
    for target_atlas, sources in atlas_sources.items():
        if not sources:
            continue
        atlas_name = "items" if target_atlas == "item" else "blocks"
        atlas_path = assets_dir / "minecraft" / "atlases" / f"{atlas_name}.json"
        ensure_dir(atlas_path.parent)
        atlas_data = {
            "sources": [
                {
                    "type": "minecraft:single",
                    "resource": resource,
                    "sprite": sprite,
                }
                for resource, sprite in sorted(sources)
            ]
        }
        write_json_pretty(atlas_path, atlas_data)
        log(f"Added {len(sources)} texture aliases to {atlas_path}")

# ---------------------------
# Model JSON texture rewriting
# ---------------------------

TEXTURE_ATLAS_ROOTS = {"block", "item"}
TARGET_RESOURCE_PACK_FORMAT = 75

def _strip_png_suffix(path):
    return path[:-4] if path.lower().endswith(".png") else path

def _normalise_texture_path(value):
    v = value.strip().replace("\\", "/")
    local = False
    if v.startswith("./"):
        local = True
        v = v[2:]
    return local, _strip_png_suffix(v)

def _find_local_texture(src_folder, texture_path):
    """Return the local texture name copied by this patcher, if it exists."""
    path = _strip_png_suffix(texture_path).replace("\\", "/").lstrip("/")
    candidates = [
        Path(src_folder) / f"{path}.png",
        Path(src_folder) / f"{Path(path).name}.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            if candidate.parent == Path(src_folder):
                return candidate.stem
            return str(candidate.relative_to(src_folder).with_suffix("")).replace("\\", "/")
    return None

def choose_texture_atlas(textures, src_folder):
    for val in textures.values():
        if not isinstance(val, str) or ":" in val or val.strip().startswith("#"):
            continue
        _, base = _normalise_texture_path(val)
        root = base.split("/", 1)[0] if "/" in base else None
        if root in TEXTURE_ATLAS_ROOTS:
            return root
    return "item"

def rewrite_texture_value(value, src_folder, generated_folder_name, target_atlas):
    if not isinstance(value, str):
        return value

    raw = value.strip()
    if not raw or raw.startswith("#"):
        return value

    local, base = _normalise_texture_path(raw)
    if ":" in base:
        return value

    root = base.split("/", 1)[0] if "/" in base else None
    local_name = _find_local_texture(src_folder, base)

    if local or local_name:
        texture_name = local_name or base
        if root in TEXTURE_ATLAS_ROOTS and "/" in texture_name:
            texture_name = texture_name.split("/", 1)[1]
        return f"{generated_folder_name}:{target_atlas}/{texture_name}"

    if root in TEXTURE_ATLAS_ROOTS:
        return f"minecraft:{base}"

    if re.search(r"[A-Za-z_]", base):
        return f"{generated_folder_name}:{target_atlas}/{base}"

    return value

def ensure_particle_texture(model_data):
    """Give custom geometry a valid particle texture for modern model loading."""
    if not isinstance(model_data, dict):
        return

    textures = model_data.get("textures")
    if not isinstance(textures, dict) or "particle" in textures:
        return

    preferred_keys = ("all", "layer0", "0")
    candidates = [textures.get(key) for key in preferred_keys]
    candidates.extend(textures.values())
    particle = next(
        (value for value in candidates
         if isinstance(value, str) and value and not value.startswith("#")),
        None,
    )
    if particle:
        textures["particle"] = particle

def remove_unresolved_texture_faces(model_data):
    """Treat legacy undefined texture references as intentionally hidden faces."""
    if not isinstance(model_data, dict):
        return

    textures = model_data.get("textures")
    texture_keys = set(textures) if isinstance(textures, dict) else set()
    for element in model_data.get("elements", []):
        if not isinstance(element, dict):
            continue
        faces = element.get("faces")
        if not isinstance(faces, dict):
            continue
        for side, face in list(faces.items()):
            if not isinstance(face, dict):
                continue
            texture = face.get("texture")
            unresolved = (
                not isinstance(texture, str)
                or not texture
                or texture.lower() == "null"
                or (texture.startswith("#") and texture[1:] not in texture_keys)
            )
            if unresolved:
                del faces[side]

def patch_pack_mcmeta(dest_root):
    mcmeta_path = Path(dest_root) / "pack.mcmeta"
    data = safe_load_json(mcmeta_path)
    if not isinstance(data, dict):
        data = {}

    pack = data.setdefault("pack", {})
    description = pack.get("description", "Patched CIT resource pack")
    pack.clear()
    pack.update({
        "pack_format": TARGET_RESOURCE_PACK_FORMAT,
        "min_format": [TARGET_RESOURCE_PACK_FORMAT, 0],
        "max_format": TARGET_RESOURCE_PACK_FORMAT,
        "description": f"{description}\nPatched for Minecraft 1.21.11"
    })
    write_json_pretty(mcmeta_path, data)
    log(f"Updated pack.mcmeta for resource pack format {TARGET_RESOURCE_PACK_FORMAT}")

def copy_texture_with_mcmeta(src_path, dest):
    shutil.copy2(str(src_path), dest)
    log(f"Copied PNG {src_path} -> {dest}")

    src_mcmeta = Path(f"{src_path}.mcmeta")
    if src_mcmeta.exists():
        dest_mcmeta = Path(f"{dest}.mcmeta")
        shutil.copy2(str(src_mcmeta), str(dest_mcmeta))
        log(f"Copied PNG metadata {src_mcmeta} -> {dest_mcmeta}")

def copy_emissive_properties(src_root, generated_asset_root):
    src = Path(src_root) / "assets" / "minecraft" / "optifine" / "emissive.properties"
    if not src.exists():
        return

    dest = Path(generated_asset_root) / "optifine" / "emissive.properties"
    ensure_dir(dest.parent)
    shutil.copy2(str(src), str(dest))
    log(f"Copied emissive properties {src} -> {dest}")

# Rewrite model JSON textures to use generated namespace
def rewrite_model_textures_and_write(src_json_path, dest_json_path, generated_folder_name):
    try:
        data = json.loads(Path(src_json_path).read_text(encoding='utf-8'))
    except Exception as e:
        log(f"Couldn't parse JSON {src_json_path}: {e}. Copying raw file.")
        shutil.copy2(src_json_path, dest_json_path)
        return

    # --- Resolve parent display transforms ---
    data = resolve_model_parents(data, Path(src_json_path).parent)

    # --- Rewrite textures ---
    if isinstance(data, dict) and "textures" in data and isinstance(data["textures"], dict):
        textures = data["textures"]
        target_atlas = choose_texture_atlas(textures, Path(src_json_path).parent)
        for key, val in list(textures.items()):
            new_val = rewrite_texture_value(val, Path(src_json_path).parent, generated_folder_name, target_atlas)
            if new_val != val:
                textures[key] = new_val
                log(f"Rewrote texture '{val}' -> '{new_val}' in {src_json_path}")

        ensure_particle_texture(data)
        remove_unresolved_texture_faces(data)

    write_json_pretty(dest_json_path, data)

# Resolves texture key references like "#trapdoor" -> the actual texture path.
def resolve_texture_references(textures):
    resolved = dict(textures)
    changed = True
    while changed:
        changed = False
        for key, val in list(resolved.items()):
            if isinstance(val, str) and val.startswith("#"):
                ref_key = val[1:]
                if ref_key in resolved and resolved[ref_key] != val:
                    resolved[key] = resolved[ref_key]
                    changed = True
    return resolved

# Recursively resolve parent models and merge fields, child overrides parent.
def resolve_model_parents(data, src_folder, visited=None):
    if not isinstance(data, dict):
        return data

    if visited is None:
        visited = set()

    parent_path = data.get("parent")
    if not parent_path or not parent_path.startswith("./"):
        return data

    parent_file = Path(src_folder) / (Path(parent_path).stem + ".json")

    # Prevent infinite loops
    if parent_file in visited:
        log(f"Cycle detected for {parent_file}, skipping")
        return data
    visited.add(parent_file)

    if not parent_file.exists():
        log(f"Parent {parent_file} not found")
        data.pop("parent", None)
        return data

    try:
        parent_data = json.loads(parent_file.read_text(encoding='utf-8'))
        parent_data = resolve_model_parents(parent_data, src_folder, visited)

        # --- Merge all relevant fields ---
        merged = dict(parent_data)  # start from parent copy
        merged.update({k: v for k, v in data.items() if k not in ("textures", "elements", "display")})

        # Merge textures (child overrides individual keys)
        if "textures" in parent_data or "textures" in data:
            merged["textures"] = dict(parent_data.get("textures", {}))
            merged["textures"].update(data.get("textures", {}))

        # Merge elements (child replaces entirely if present)
        if "elements" in data:
            merged["elements"] = data["elements"]
        elif "elements" in parent_data:
            merged["elements"] = parent_data["elements"]

        # Merge display (child replaces or extends)
        if "display" in parent_data or "display" in data:
            merged["display"] = dict(parent_data.get("display", {}))
            merged["display"].update(data.get("display", {}))

        # Ambient occlusion, etc.
        if "ambientocclusion" not in merged and "ambientocclusion" in parent_data:
            merged["ambientocclusion"] = parent_data["ambientocclusion"]

        # Remove parent to avoid unresolved reference
        merged.pop("parent", None)

        # Resolve #texture references after merging
        if "textures" in merged:
            merged["textures"] = resolve_texture_references(merged["textures"])

        return merged

    except Exception as e:
        log(f"Error resolving parent {parent_path}: {e}")
        data.pop("parent", None)
        return data

# ---------------------------
# CIT processing
# ---------------------------

# Correct fallbacks for special items/blocks
FALLBACK_OVERRIDES = {
    "cake": "minecraft:item/cake",  # standard item model (not block)

    # "grass" was renamed to "short_grass" after this 1.20.1 CIT pack.
    "short_grass": {
        "type": "minecraft:model",
        "model": "minecraft:item/short_grass",
        "tints": [
            {
                "type": "minecraft:grass",
                "downfall": 1.0,
                "temperature": 0.5,
            }
        ],
    },

    # Shields – special renderer
    "shield": {
        "type": "minecraft:special",
        "base": "minecraft:item/shield",
        "model": {"type": "minecraft:shield"}
    },

    # Chests use special block entity model
    "chest": {
        "type": "minecraft:special",
        "base": "minecraft:item/chest",
        "model": {
            "type": "minecraft:chest",
            "texture": "minecraft:normal"
        }
    },

    # Ominous banner special variant
    "ominous_banner": {
        "type": "minecraft:special",
        "base": "minecraft:item/banner",
        "model": {"type": "minecraft:banner", "pattern_color": "black"}
    },
}

# Add all fence, trapdoor, and sign variants
for wood in ["oak", "spruce", "birch", "jungle", "acacia", "dark_oak",
             "mangrove", "cherry", "bamboo", "crimson", "warped",
             "nether_brick", "pale_oak"]:
    FALLBACK_OVERRIDES[f"{wood}_fence"] = f"minecraft:block/{wood}_fence_inventory"
    FALLBACK_OVERRIDES[f"{wood}_trapdoor"] = f"minecraft:block/{wood}_trapdoor_bottom"
    FALLBACK_OVERRIDES[f"{wood}_sign"] = f"minecraft:item/{wood}_sign"

# Add colored beds and banners (special renderers)
COLORS = ["white", "orange", "magenta", "light_blue", "yellow", "lime", "pink",
          "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"]

for color in COLORS:
    # Beds use special block entity model
    FALLBACK_OVERRIDES[f"{color}_bed"] = {
        "type": "minecraft:special",
        "base": "minecraft:item/template_bed",
        "model": {
            "type": "minecraft:bed",
            "texture": f"minecraft:{color}"
        }
    }

    # Banners use special block entity model
    FALLBACK_OVERRIDES[f"{color}_banner"] = {
        "type": "minecraft:special",
        "base": "minecraft:item/template_banner",
        "model": {
            "type": "minecraft:banner",
            "color": color
        }
    }

# Mob heads use special block entity models
for mob in ["zombie", "creeper", "player", "dragon", "piglin"]:
    FALLBACK_OVERRIDES[f"{mob}_head"] = {
        "type": "minecraft:special",
        "base": "minecraft:item/template_skull",
        "model": {"type": "minecraft:head", "kind": mob}
    }

for mob in ["skeleton", "wither_skeleton"]:
    FALLBACK_OVERRIDES[f"{mob}_skull"] = {
    "type": "minecraft:special",
    "base": "minecraft:item/template_skull",
    "model": {"type": "minecraft:head", "kind": mob}
}

# Clock special case: range dispatch based on time of day
FALLBACK_OVERRIDES["clock"] = {
    "type": "select",
    "cases": [
        {
            "when": "overworld",
            "model": {
                "type": "range_dispatch",
                "entries": [
                    {"model": {"type": "model", "model": f"item/clock_{i:02}"}, "threshold": t}
                    for i, t in enumerate(
                        [0.0] + [i + 0.5 for i in range(1, 63)] + [64.0]
                    )
                ],
                "property": "time",
                "scale": 64,
                "source": "daytime"
            }
        }
    ],
    "fallback": {
        "type": "range_dispatch",
        "entries": [
            {"model": {"type": "model", "model": f"item/clock_{i:02}"}, "threshold": t}
            for i, t in enumerate(
                [0.0] + [i + 0.5 for i in range(1, 63)] + [64.0]
            )
        ],
        "property": "time",
        "scale": 64,
        "source": "random"
    },
    "property": "context_dimension"
}

HANDHELD_ITEM_SUFFIXES = (
    "_sword", "_axe", "_pickaxe", "_shovel", "_hoe",
    "_trident", "_mace"
)

ITEM_ID_ALIASES = {
    "grass": "short_grass",
}

def default_texture_model_parent(item_name):
    if item_name == "bow" or item_name == "crossbow" or item_name.endswith(HANDHELD_ITEM_SUFFIXES):
        return "minecraft:item/handheld"
    return "minecraft:item/generated"

def write_texture_only_model(dest_model_path, item_name, texture_field, generated_folder_name):
    if os.path.exists(dest_model_path):
        log(f"Skipping existing texture-only model {dest_model_path}")
        return

    texture_name = Path(texture_field.replace("\\", "/")).stem
    model = {
        "parent": default_texture_model_parent(item_name),
        "textures": {
            "layer0": f"{generated_folder_name}:item/{texture_name}",
            "particle": f"{generated_folder_name}:item/{texture_name}",
        }
    }
    write_json_pretty(dest_model_path, model)
    log(f"Generated texture-only model {dest_model_path}")

# Process files based on type
def process_cit_file(src_path, dest_items_path, generated_asset_root, generated_folder_name, block_names):
    src_path = Path(src_path)
    lower = src_path.suffix.lower()
    if lower == ".properties":
        props = parse_properties(str(src_path))
        # matchItems can be missing variants - check multiple keys
        match_items_value = props.get("matchItems") or props.get("match_items") or props.get("matchitems")
        if not match_items_value:
            log(f"No matchItems in {src_path}; skipping")
            return
        # process each token
        tokens = re.split(r'\s+', match_items_value.strip())
        # model field
        model_field = props.get("model") or props.get("Model") or props.get("model-file")
        texture_field = props.get("texture") or props.get("Texture")
        if not model_field and not texture_field:
            log(f"No model= or texture= in {src_path}; skipping")
            return
        resource_field = model_field if model_field else texture_field
        if resource_field is None:
            log(f"No model= or texture= in {src_path}; skipping")
            return
        model_name = Path(resource_field).stem  # e.g., apple_0
        prop_stem = src_path.stem
        for tok in tokens:
            if not tok:
                continue
            if ':' in tok:
                ns, item_name = tok.split(':', 1)
            else:
                ns, item_name = 'minecraft', tok

            if ns == "minecraft" and item_name in ITEM_ID_ALIASES:
                old_item_name = item_name
                item_name = ITEM_ID_ALIASES[item_name]
                log(f"Remapped item ID minecraft:{old_item_name} -> minecraft:{item_name}")

            # compute case_when now that we know the property stem AND the item_name
            case_when = transform_name_to_vanilla(prop_stem)

            # If this is a colored bed or banner, prepend the color name (e.g. "Brown Bed_0")
            for color in COLORS:
                if item_name == f"{color}_bed" or item_name == f"{color}_banner":
                    case_when = f"{color.title()} {case_when}"
                    break

            # decide fallback block/item via block_names set
            if item_name in FALLBACK_OVERRIDES:
                fallback = FALLBACK_OVERRIDES[item_name]
            elif item_name in block_names:
                fallback = f"minecraft:block/{item_name}"
            else:
                fallback = f"minecraft:item/{item_name}"

            item_json_path = os.path.join(dest_items_path, f"{item_name}.json")
            if not model_field and texture_field:
                dest_model_path = os.path.join(generated_asset_root, "models", "item", f"{model_name}.json")
                write_texture_only_model(dest_model_path, item_name, texture_field, generated_folder_name)

            case_model_path = f"{generated_folder_name}:item/{model_name}"
            merge_item_json(item_json_path, case_when, case_model_path, fallback)
            log(f"Added/updated case '{case_when}' -> {case_model_path} to {item_json_path}")

    elif lower == ".png":
        for atlas in ("item", "block"):
            dest = os.path.join(generated_asset_root, "textures", atlas, src_path.name)
            if os.path.exists(dest):
                log(f"Skipping existing texture {dest}")
            else:
                ensure_dir(os.path.dirname(dest))
                copy_texture_with_mcmeta(src_path, dest)

    elif lower == ".json":
        # when copying model JSONs, rewrite textures if necessary
        dest = os.path.join(generated_asset_root, "models", "item", src_path.name)
        if os.path.exists(dest):
            log(f"Skipping existing model {dest}")
        else:
            rewrite_model_textures_and_write(str(src_path), dest, generated_folder_name)
            log(f"Copied/rewrote model JSON {src_path} -> {dest}")
    else:
        log(f"Ignored CIT file type: {src_path}")

# ---------------------------
# Main pack processing (zip or folder)
# ---------------------------

# Process a resource pack (zip or folder)
def process_pack(input_path, generated_folder_name, block_names):
    input_path = Path(input_path)
    base_name = input_path.stem
    output_name = f"Patched {base_name}"
    temp_dir = None

    # If zip: extract to temp folder
    if zipfile.is_zipfile(input_path):
        temp_dir = tempfile.mkdtemp(prefix="cit_unpack_")
        with zipfile.ZipFile(input_path, "r") as z:
            z.extractall(temp_dir)
        src_root = Path(temp_dir)
    else:
        src_root = input_path

    dest_root = input_path.parent / output_name
    assets_dir = dest_root / "assets"
    items_dir = assets_dir / "minecraft" / "items"
    generated_asset_root = assets_dir / generated_folder_name

    ensure_dir(items_dir)
    ensure_dir(generated_asset_root / "models" / "item")
    ensure_dir(generated_asset_root / "textures" / "item")
    ensure_dir(generated_asset_root / "textures" / "block")

    # Copy root-level non-assets files/dirs
    copy_root_files(str(src_root), str(dest_root))
    patch_pack_mcmeta(dest_root)
    copy_emissive_properties(src_root, generated_asset_root)

    # Walk CIT source
    cit_dir = src_root / "assets" / "minecraft" / "optifine" / "cit"
    if not cit_dir.exists():
        log(f"No CIT folder found at {cit_dir}; nothing to do.")
        # cleanup if zip input
        if temp_dir:
            shutil.rmtree(temp_dir)
        return

    for root, _, files in os.walk(str(cit_dir)):
        for f in files:
            src = Path(root) / f
            process_cit_file(str(src), str(items_dir), str(generated_asset_root), generated_folder_name, block_names)

    normalise_item_case_atlases(items_dir, generated_asset_root, generated_folder_name)

    # If input was zip: pack back to zip and cleanup extracted folder and temporary dest folder
    if temp_dir:
        output_zip = str(dest_root) + ".zip"
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(str(dest_root)):
                for f in files:
                    absf = os.path.join(root, f)
                    relf = os.path.relpath(absf, str(dest_root))
                    z.write(absf, relf)
        # cleanup
        shutil.rmtree(temp_dir)
        shutil.rmtree(str(dest_root))
        log(f"Created patched zip: {output_zip}")
        print(f"Patched pack created: {output_zip}")
    else:
        log(f"Created patched folder: {dest_root}")
        print(f"Patched pack created: {dest_root}")

# ---------------------------
# Entry point
# ---------------------------

def main():
    global GLOBAL_CONFIG
    GLOBAL_CONFIG = load_config()

    block_names = load_block_names()

    # Accept argument path or prompt
    if len(sys.argv) >= 2:
        input_path = sys.argv[1]
    else:
        input_path = input("Enter path to resource pack (.zip or folder): ").strip().strip('"')

    if not input_path:
        print("No path provided. Exiting.")
        return
    if not os.path.exists(input_path):
        print("Path does not exist:", input_path)
        return

    # Prompt user for generated folder name if enabled
    generated_folder_name = GLOBAL_CONFIG["generated_folder_name"]
    if GLOBAL_CONFIG.get("prompt_for_generated_name", False):
        user_input = input(f"Enter generated folder name [{generated_folder_name}]: ").strip()
        if user_input:
            generated_folder_name = user_input

    process_pack(input_path, generated_folder_name, block_names)

if __name__ == "__main__":
    main()
