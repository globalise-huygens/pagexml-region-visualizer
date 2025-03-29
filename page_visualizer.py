#!/usr/bin/env python3
"""
PAGE Region Visualization Tool

This script processes PAGE-format XML files and their corresponding JPG images to 
visualize text regions with color-coded overlays. It supports processing single files 
or batch processing with statistics on region counts and region sequences. In the visual overlays, the label now shows the region’s layout name along with its reading order number and the total number of regions (e.g. "header (1/8)").

By default:
    - In batch mode, the script creates two TSV files:
          * region_counts.tsv: Contains statistics with columns:
                filename, total_regions, and one count column per region type (e.g., count_header).
          * region_sequences.tsv: Contains statistics with columns:
                filename, total_regions, last_region, region_sequence (comma-separated layout names).
    - In single file mode these files are not created unless the --stats option is provided.

Usage:
    Single file:
        python page_visualizer.py <base_filename> [--font-size=48] [--stats]
    Batch mode:
        python page_visualizer.py --all [--font-size=48] [--no-overlays] [--no-stats]

Note for batch mode:
    - The script assumes that identically named PageXML and JPG scans will be present in the xml/ and images/ directories.
    - Use the --no-overlays option to prevent overlay images from being created.
    - Use the --no-stats option to suppress the default generation of region_counts.tsv and region_sequences.tsv.
"""

import os
import sys
import re
import argparse
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from PIL import Image, ImageDraw, ImageColor, ImageFont
from concurrent.futures import ProcessPoolExecutor, as_completed

# Configuration
@dataclass
class Config:
    """Configuration settings for the visualization tool."""
    IMAGES_DIR: Path = Path("images")
    XML_DIR: Path = Path("xml")
    OUTPUT_DIR: Path = Path("output")
    DEFAULT_FONT_SIZE: int = 60
    DEFAULT_STATS_FILE: str = "region_counts.tsv"
    DEFAULT_SEQUENCE_FILE: str = "region_sequences.tsv"  # File for region sequence data
    
    # Region type color mapping
    REGION_COLORS: Dict[str, str] = field(default_factory=lambda: {
        'header': 'red',
        'paragraph': 'blue',
        'catch-word': 'green',
        'page-number': 'yellow',
        'marginalia': 'purple',
        'signature-mark': 'orange'
    })
    DEFAULT_COLOR: str = 'pink'

    # XML namespace
    PAGE_NS: Dict[str, str] = field(default_factory=lambda: {
        'pc': 'https://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15'
    })


class RegionProcessor:
    """Handles the processing of PAGE XML regions and overlay generation."""
    
    def __init__(self, config: Config, font_size: Optional[int] = None):
        self.config = config
        self.font_size = font_size or config.DEFAULT_FONT_SIZE
        self.font = self._initialize_font()
    
    def _initialize_font(self) -> ImageFont.FreeTypeFont:
        """Initialize font for region labels."""
        font_options = [
            "Arial.ttf",
            "DejaVuSans.ttf",
            "FreeSans.ttf",
            "NotoSans-Regular.ttf"
        ]
        system_font_paths = [
            Path("/usr/share/fonts/truetype"),
            Path("/System/Library/Fonts"),
            Path("C:/Windows/Fonts")
        ]
        for font_name in font_options:
            try:
                return ImageFont.truetype(font_name, self.font_size)
            except IOError:
                pass
            for path in system_font_paths:
                font_path = path / font_name
                if font_path.exists():
                    try:
                        return ImageFont.truetype(str(font_path), self.font_size)
                    except IOError:
                        continue
        logging.warning("Could not find a suitable TrueType font. Using default font, which may affect text display.")
        try:
            return ImageFont.load_default().font_variant(size=self.font_size)
        except (AttributeError, IOError):
            return ImageFont.load_default()

    @staticmethod
    def extract_region_type(custom_attr: Optional[str]) -> Optional[str]:
        if not custom_attr:
            return None
        if match := re.search(r'type\s*:\s*(\w+(-\w+)*)', custom_attr):
            return match.group(1).lower()
        return None

    @staticmethod
    def get_region_type(region: ET.Element) -> str:
        custom_attr = region.get('custom', '')
        region_type = RegionProcessor.extract_region_type(custom_attr)
        if region_type is None:
            region_type = region.get('type', 'unknown').lower()
        return region_type

    @staticmethod
    def extract_reading_order(region: ET.Element) -> Optional[int]:
        """
        Extracts the reading order index from the region's custom attribute.
        Returns a 1-indexed value if found.
        """
        custom_attr = region.get('custom', '')
        if custom_attr:
            if match := re.search(r'readingOrder\s*\{\s*index\s*:\s*(\d+)', custom_attr):
                return int(match.group(1)) + 1
        return None

    @staticmethod
    def parse_coords(points_str: Optional[str]) -> List[Tuple[int, int]]:
        points = []
        if not points_str:
            return points
        for point in points_str.strip().split():
            try:
                x_str, y_str = point.split(',')
                points.append((int(float(x_str)), int(float(y_str))))
            except (ValueError, TypeError) as e:
                logging.warning(f"Invalid coordinate in '{point}': {e}")
                continue
        return points

    def create_overlay(self, image_size: Tuple[int, int], regions: List[ET.Element], namespace: Dict[str, str]) -> Image.Image:
        overlay = Image.new("RGBA", image_size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        total_regions = len(regions)
        for region in regions:
            self._process_region(draw, region, namespace, total_regions)
        return overlay

    def _process_region(self, draw: ImageDraw.Draw, region: ET.Element, namespace: Dict[str, str], total_regions: int) -> None:
        region_type = RegionProcessor.get_region_type(region)
        reading_order = RegionProcessor.extract_reading_order(region)
        base_color = self.config.REGION_COLORS.get(region_type, self.config.DEFAULT_COLOR)
        try:
            rgb = ImageColor.getrgb(base_color)
            fill_color = (*rgb, 100)  # Semi-transparent fill
            outline_color = (*rgb, 255)  # Solid outline
        except ValueError:
            logging.warning(f"Invalid color '{base_color}' for region type '{region_type}'. Using default.")
            rgb = ImageColor.getrgb(self.config.DEFAULT_COLOR)
            fill_color = (*rgb, 100)
            outline_color = (*rgb, 255)

        coords_elem = region.find('.//pc:Coords', namespace)
        if coords_elem is None:
            logging.warning(f"No Coords element found for region {region.get('id', 'unknown')}")
            return
            
        points = self.parse_coords(coords_elem.get('points', ''))
        if not points:
            logging.warning(f"No valid points found for region {region.get('id', 'unknown')}")
            return
            
        if len(points) >= 3:
            self._draw_region(draw, points, fill_color, outline_color, region_type, reading_order, total_regions)
        else:
            logging.warning(f"Not enough points ({len(points)}) to draw region {region.get('id', 'unknown')}")

    def _draw_region(self, draw: ImageDraw.Draw, points: List[Tuple[int, int]], 
                     fill_color: Tuple[int, ...], outline_color: Tuple[int, ...], 
                     region_type: str, reading_order: Optional[int], total_regions: int) -> None:
        # Draw filled polygon without an outline
        draw.polygon(points, fill=fill_color)
        # Draw thicker outline by drawing lines between points
        for i in range(len(points)):
            start = points[i]
            end = points[(i + 1) % len(points)]
            draw.line([start, end], fill=outline_color, width=3)
        for point in points:
            self._draw_vertex_marker(draw, point, outline_color)
        self._draw_region_label(draw, points, region_type, outline_color, reading_order, total_regions)

    def _draw_vertex_marker(self, draw: ImageDraw.Draw, point: Tuple[int, int], 
                            color: Tuple[int, ...], radius: int = 5) -> None:
        x, y = point
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    def _draw_region_label(self, draw: ImageDraw.Draw, points: List[Tuple[int, int]], 
                           label_text: str, color: Tuple[int, ...],
                           reading_order: Optional[int], total_regions: int) -> None:
        center_x = sum(p[0] for p in points) // len(points)
        center_y = sum(p[1] for p in points) // len(points)
        if reading_order is not None:
            label = f"{label_text} ({reading_order}/{total_regions})"
        else:
            label = f"{label_text}"
        offsets = [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]
        for offset_x, offset_y in offsets:
            draw.text((center_x + offset_x, center_y + offset_y), label, font=self.font, fill=(0, 0, 0, 255))
        rgb_only = color[:3]
        brightness = sum(c * w for c, w in zip(rgb_only, (299, 587, 114))) / 1000
        text_color = (0, 0, 0, 255) if brightness > 128 else (255, 255, 255, 255)
        draw.text((center_x, center_y), label, font=self.font, fill=text_color)


class PageVisualizer:
    """Main class for PAGE document visualization."""
    
    def __init__(self, config: Config):
        self.config = config
        self.stats_data = []
        self._ensure_directories()
        
    def _ensure_directories(self) -> None:
        for dir_path in [self.config.IMAGES_DIR, self.config.XML_DIR, self.config.OUTPUT_DIR]:
            dir_path.mkdir(parents=True, exist_ok=True)
            if not dir_path.exists():
                logging.warning(f"Could not create directory {dir_path}")
        
    def process_file(self, base_name: str, font_size: int, 
                     collect_stats: bool = False, create_overlay: bool = True) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        image_path = self.config.IMAGES_DIR / f"{base_name}.jpg"
        xml_path = self.config.XML_DIR / f"{base_name}.xml"
        
        if not self._verify_files(image_path, xml_path):
            return False, None, None
            
        try:
            image = Image.open(image_path).convert("RGBA")
            processor = RegionProcessor(self.config, font_size)
            tree = ET.parse(xml_path)
            root = tree.getroot()
            namespace = self._update_namespace(root)
            regions = root.findall('.//pc:TextRegion', namespace)
            if not regions:
                logging.warning(f"No TextRegion elements found in {xml_path}")
            
            if create_overlay:
                overlay = processor.create_overlay(image.size, regions, namespace)
                result = Image.alpha_composite(image, overlay).convert("RGB")
                self._save_result(result, base_name)
            else:
                logging.info(f"Overlay creation skipped for {base_name}")
            
            stats = self._collect_statistics(regions, base_name) if collect_stats else None

            # Always extract the region sequence
            sequence = self.extract_region_sequence(root, namespace)
                
            return True, stats, sequence
            
        except Exception as e:
            logging.error(f"Error processing {base_name}: {e}")
            return False, None, None

    def _verify_files(self, image_path: Path, xml_path: Path) -> bool:
        if not image_path.exists():
            logging.warning(f"Image file not found: {image_path}")
            return False
        if not xml_path.exists():
            logging.warning(f"XML file not found: {xml_path}")
            return False
        return True

    def _update_namespace(self, root: ET.Element) -> Dict[str, str]:
        if match := re.search(r'\{(.+?)\}', root.tag):
            namespace_uri = match.group(1)
            return {'pc': namespace_uri}
        for attr, value in root.attrib.items():
            if attr.startswith('xmlns:'):
                prefix = attr.split(':')[1]
                return {prefix: value}
        logging.warning("No namespace found in XML. Using default namespace.")
        return self.config.PAGE_NS

    def _save_result(self, image: Image.Image, base_name: str) -> None:
        output_path = self.config.OUTPUT_DIR / f"{base_name}_overlay.jpg"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)
        logging.info(f"Saved overlay image: {output_path}")

    def _collect_statistics(self, regions: List[ET.Element], 
                            base_name: str) -> Dict[str, Any]:
        region_counts = {}
        for region in regions:
            region_type = RegionProcessor.get_region_type(region)
            region_counts[region_type] = region_counts.get(region_type, 0) + 1
        return {
            'filename': base_name,
            'total_regions': len(regions),
            'region_counts': region_counts
        }
    
    def extract_region_sequence(self, root: ET.Element, namespace: Dict[str, str]) -> Dict[str, Any]:
        """
        Extracts the reading order of regions as layout region names.
        Builds a mapping from region ID to region type, then determines the 
        reading order from the <ReadingOrder>/<OrderedGroup> element.
        Returns a dictionary with:
            - region_sequence: a list of layout region names in reading order.
            - total_regions: count of all <TextRegion> elements.
            - last_region: the layout name of the final region in the reading order.
        """
        regions = root.findall('.//pc:TextRegion', namespace)
        total_regions = len(regions)
        id_to_type = {}
        for region in regions:
            region_id = region.get('id')
            if region_id:
                id_to_type[region_id] = RegionProcessor.get_region_type(region)
                
        sequence = []
        reading_order = root.find('.//pc:ReadingOrder', namespace)
        if reading_order is not None:
            ordered_group = reading_order.find('.//pc:OrderedGroup', namespace)
            if ordered_group is not None:
                for region_ref in ordered_group.findall('.//pc:RegionRefIndexed', namespace):
                    region_id = region_ref.get('regionRef')
                    if region_id:
                        sequence.append(id_to_type.get(region_id, region_id))
        if not sequence:
            # Fallback: use the order of TextRegion elements.
            for region in regions:
                region_id = region.get('id')
                if region_id:
                    sequence.append(id_to_type.get(region_id, region_id))
        last_region = sequence[-1] if sequence else ''
        return {
            'region_sequence': sequence,
            'total_regions': total_regions,
            'last_region': last_region
        }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process PAGE XML files and generate region visualizations, statistics, and reading order files."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("base_name", nargs="?", help="Base name of the file to process")
    group.add_argument("--all", action="store_true", help="Process all XML files in the directory")
    # Allow custom font size for both modes.
    parser.add_argument("--font-size", type=int, default=Config.DEFAULT_FONT_SIZE,
                        help=f"Font size for region labels (default: {Config.DEFAULT_FONT_SIZE})")
    # In batch mode add options for overlays and statistics; otherwise, add --stats.
    if '--all' in sys.argv:
        parser.add_argument("--no-overlays", action="store_true", help="Do not create overlay images")
        parser.add_argument("--no-stats", action="store_true", help="Do not create region_counts.tsv and region_sequences.tsv files")
    else:
        parser.add_argument("--stats", action="store_true", help="Generate region_counts.tsv and region_sequences.tsv files")
    return parser.parse_args()


def generate_stats_file(stats_data: List[Dict[str, Any]], filename: str, config: Config, silent: bool = False) -> None:
    output_path = config.OUTPUT_DIR / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with output_path.open('w', newline='') as f:
            region_types = set()
            for stat in stats_data:
                region_types.update(stat['region_counts'].keys())
            sorted_region_types = sorted(region_types)
            header = ["filename", "total_regions"] + [f"count_{type_}" for type_ in sorted_region_types]
            f.write('\t'.join(header) + '\n')
            for stat in stats_data:
                row = [
                    stat['filename'],
                    str(stat['total_regions'])
                ] + [str(stat['region_counts'].get(type_, 0)) for type_ in sorted_region_types]
                f.write('\t'.join(row) + '\n')
        if not silent:
            logging.info(f"Statistics written to {output_path}")
    except Exception as e:
        logging.error(f"Error writing statistics: {e}")


def generate_sequence_file(sequence_data: List[Dict[str, Any]], filename: str, config: Config, silent: bool = False) -> None:
    output_path = config.OUTPUT_DIR / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with output_path.open('w', newline='') as f:
            header = ["filename", "total_regions", "last_region", "region_sequence"]
            f.write('\t'.join(header) + '\n')
            for record in sequence_data:
                # Join the region names with commas.
                sequence_str = ','.join(record['region_sequence'])
                row = [
                    record['filename'],
                    str(record['total_regions']),
                    record['last_region'],
                    sequence_str
                ]
                f.write('\t'.join(row) + '\n')
        if not silent:
            logging.info(f"Region sequence data written to {output_path}")
    except Exception as e:
        logging.error(f"Error writing region sequence data: {e}")


def process_file_process(base_name: str, args: argparse.Namespace, config: Config) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Process a single file in a separate process.
    A new PageVisualizer is created here to avoid pickling issues.
    """
    visualizer = PageVisualizer(config)
    # Allow font size to be set from arguments in both modes.
    font_size = args.font_size
    # Determine whether to create overlays based on --no-overlays.
    create_overlay = not getattr(args, "no_overlays", False)
    return visualizer.process_file(base_name, font_size, True, create_overlay)


def process_all_files(visualizer: PageVisualizer, args: argparse.Namespace) -> None:
    # Sort the xml files alphabetically as in ls
    xml_files = sorted(visualizer.config.XML_DIR.glob('*.xml'), key=lambda f: f.name)
    if not xml_files:
        logging.error("No XML files found.")
        sys.exit(1)
    
    logging.info(f"Processing {len(xml_files)} files...")
    success_count = 0
    stats_data = {}
    sequence_data = []
    
    with ProcessPoolExecutor() as executor:
        futures = {
            executor.submit(process_file_process, xml_file.stem, args, visualizer.config): xml_file
            for xml_file in xml_files
        }
        for future in as_completed(futures):
            try:
                success, stats, sequence = future.result()
            except Exception as e:
                logging.error(f"Error processing {futures[future].stem}: {e}")
            else:
                if success:
                    success_count += 1
                    if stats:
                        stats_data.setdefault("data", []).append(stats)
                    if sequence is not None:
                        sequence_data.append({
                            'filename': futures[future].stem,
                            'total_regions': sequence['total_regions'],
                            'last_region': sequence['last_region'],
                            'region_sequence': sequence['region_sequence']
                        })
    
    # Sort the results by filename so they appear in the same order as ls.
    if "data" in stats_data:
        stats_data["data"].sort(key=lambda x: x["filename"])
    if sequence_data:
        sequence_data.sort(key=lambda x: x["filename"])
    
    logging.info(f"Processed {success_count} of {len(xml_files)} files successfully")
    
    if not args.no_stats:
        # Generate statistics file.
        if "data" in stats_data:
            generate_stats_file(stats_data["data"], visualizer.config.DEFAULT_STATS_FILE, visualizer.config)
        # Generate region sequence file.
        if sequence_data:
            generate_sequence_file(sequence_data, visualizer.config.DEFAULT_SEQUENCE_FILE, visualizer.config)


def process_single_file(visualizer: PageVisualizer, args: argparse.Namespace) -> None:
    font_size = args.font_size  # Available in single file mode.
    success, stats, sequence = visualizer.process_file(args.base_name, font_size, collect_stats=True, create_overlay=True)
    if success:
        if args.stats:
            if stats:
                generate_stats_file([stats], visualizer.config.DEFAULT_STATS_FILE, visualizer.config, silent=True)
            if sequence is not None:
                generate_sequence_file([{
                    'filename': args.base_name,
                    'total_regions': sequence['total_regions'],
                    'last_region': sequence['last_region'],
                    'region_sequence': sequence['region_sequence']
                }], visualizer.config.DEFAULT_SEQUENCE_FILE, visualizer.config, silent=True)
        logging.info("Processing complete")
        if args.stats:
            logging.info("Statistics written to output/region_counts.tsv and output/region_sequences.tsv")
    else:
        logging.error("Processing failed")
        sys.exit(1)


def main() -> None:
    args = parse_arguments()
    
    # Configure logging: for batch mode, use a higher threshold to reduce output.
    if args.all:
        logging.basicConfig(level=logging.WARNING, format='%(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    config = Config(
        IMAGES_DIR=Path("images"),
        XML_DIR=Path("xml"),
        OUTPUT_DIR=Path("output")
    )
    
    visualizer = PageVisualizer(config)
    
    if args.all:
        process_all_files(visualizer, args)
    else:
        process_single_file(visualizer, args)


if __name__ == "__main__":
    main()
