#!/usr/bin/env python3
import os, sys
import argparse
from osgeo import gdal
import subprocess
import numpy as np
gdal.UseExceptions()

def main():
    parser = argparse.ArgumentParser(description="Apply colormap and generate tiles from a GeoTIFF.")
    parser.add_argument("input_tif", help="Path to input GeoTIFF (single-band float data)")
    parser.add_argument("colormap_file", default="viridis_r.txt", help="Path to colormap text file (e.g., magma.txt)")
    parser.add_argument("output_dir", help="Output directory for tiles (will be created if not exists)")
    parser.add_argument("-z", "--zoom", default="0-3", help="Zoom levels to generate (default: 0-4)")
    args = parser.parse_args()

    tif_path = args.input_tif
    cmap_path = os.path.abspath(args.colormap_file)
    out_dir = args.output_dir
    zoom_levels = args.zoom

    # Ensure output directory exists
    os.makedirs(out_dir, exist_ok=True)

    # Open the input GeoTIFF
    ds = gdal.Open(tif_path)
    if ds is None:
        raise RuntimeError(f"Failed to open input dataset: {tif_path}")
    
    from pprint import pprint
    info_src = gdal.Info(ds, format='json')
    #print("\nSOURCE CRS:", info_src.get('coordinateSystem', {}).get('wkt', 'NONE'))
    #print("SOURCE corners:", info_src.get('cornerCoordinates', {}))


    # 1. Translate the dataset to Byte type with scaling (vmin=-2, vmax=7 -> 1-255) and set NoData=0
    vmin=1021.5
    vmax=1027.5
    src_nd = ds.GetRasterBand(1).GetNoDataValue()
    print("src_nd:", src_nd)
    if src_nd is not None:
        translate_opts = gdal.TranslateOptions(
            format='MEM', 
            outputType=gdal.GDT_Byte,
            scaleParams=[[vmin, vmax, 1, 255]],
            noData=0,
            srcNodata=src_nd,       # map only true nodata
            dstNodata=0          
        )
    else:
        translate_opts = gdal.TranslateOptions(
            format='MEM', 
            outputType=gdal.GDT_Byte, 
            scaleParams=[[vmin, vmax, 1, 255]],
            noData=0,
        )
    

    print("Scaling raster to Byte [1-255] with clamping; NoData=0…")
    scaled_ds = gdal.Translate('', ds, options=translate_opts)
    if scaled_ds is None:
        raise RuntimeError("Scaling with gdal.Translate failed.")
    
    sb = scaled_ds.GetRasterBand(1).ReadAsArray()
    print("Zeros in scaled:", int((sb==0).sum()))  # should match NaN count in source, not “lots”
    
    # Make sure the colormap file exists
    if not os.path.exists(cmap_path):
        raise FileNotFoundError(f"Colormap file not found: {cmap_path}")

    # 2. Apply color relief using the magma colormap, adding alpha channel for transparency.
    demproc_opts = gdal.DEMProcessingOptions(
        colorFilename=cmap_path,
        addAlpha=True,
        colorSelection='nearest_color_entry',
        format='MEM'
    )
    print("Applying color relief (colormap) to create RGBA raster...")
    color_ds = gdal.DEMProcessing('', scaled_ds, 'color-relief', options=demproc_opts)
    if color_ds is None:
        raise RuntimeError("Color-relief processing failed.")
    
    # Write the color-relief output to a temporary file that gdal2tiles can read.
    temp_color_tif = "temp_color.tif"
    save_opts = gdal.TranslateOptions(format='GTiff', creationOptions=['COMPRESS=LZW','TILED=YES'])
    gdal.Translate(temp_color_tif, color_ds, options=save_opts)

    # Clean up
    color_ds = None
    scaled_ds = None

    # Build alpha from the ORIGINAL float GeoTIFF (finite -> 255, else 0)
    src_ds = gdal.Open(tif_path, gdal.GA_ReadOnly)          # keep a ref!
    if src_ds is None:
        raise RuntimeError(f"Failed to re-open source: {tif_path}")
    
    print("src_ds.RasterCount: ", src_ds.RasterCount)
    if src_ds.RasterCount == 1:
        src_arr = src_ds.GetRasterBand(1).ReadAsArray()
        valid_alpha = np.where(np.isfinite(src_arr), 255, 0).astype(np.uint8)
    else:
        # If multi-band, require all bands finite (adjust to your needs)
        valid = None
        for i in range(1, src_ds.RasterCount + 1):
            arr = src_ds.GetRasterBand(i).ReadAsArray()
            valid = np.isfinite(arr) if valid is None else (valid & np.isfinite(arr))
        valid_alpha = np.where(valid, 255, 0).astype(np.uint8)

    # Overwrite the alpha band on the colorized raster (same size at this point)
    col_ds = gdal.Open("temp_color.tif", gdal.GA_Update)
    if col_ds is None or col_ds.RasterCount < 4:
        raise RuntimeError("temp_color.tif is missing the alpha band (expected RGBA).")

    alpha_band = col_ds.GetRasterBand(4)
    alpha_band.WriteArray(valid_alpha)
    alpha_band.FlushCache()
    col_ds = None
    src_ds = None

    safe_bounds_4326 = (-180.0, -85.05112878, 180.0, 85.05112878)
    # 3. Pre-clip in EPSG:4326 to Mercator-safe latitudes
    clip_opts = gdal.WarpOptions(
        dstSRS="EPSG:4326",
        outputBounds=safe_bounds_4326,
        outputBoundsSRS="EPSG:4326",
        resampleAlg="nearest",   # RGBA/categorical → keep colors crisp
        multithread=True,
        srcAlpha=True,
        dstAlpha=True
    )
    clipped_4326 = "temp_color_clip4326.tif"
    gdal.Warp(clipped_4326, temp_color_tif, options=clip_opts)

    # 4. Reproject to EPSG:3857 (again, rely on alpha)
    warp_opts = gdal.WarpOptions(
        dstSRS="EPSG:3857",
        resampleAlg="nearest",   # still fine for tiles
        multithread=True,
        srcAlpha=True,
        dstAlpha=True
    )
    warped_tif = "temp_color_3857.tif"
    gdal.Warp(warped_tif, clipped_4326, options=warp_opts)

    # 5. Tiles
    print(f"Generating tiles (zoom levels {zoom_levels}) into: {out_dir}")
    result = subprocess.run([
        sys.executable, "-m", "osgeo_utils.gdal2tiles",
        "-p", "mercator",
        "-z", zoom_levels, 
        "-w", "none",
        warped_tif, out_dir
        ], check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"gdal2tiles failed with code {result.returncode}")
    else:
        print("Tiles have been successfully generated.")

    # Cleanup again
    if os.path.exists(temp_color_tif):
        os.remove(temp_color_tif)
    
    for f in (temp_color_tif, warped_tif):
        if os.path.exists(f):
            os.remove(f)

if __name__ == "__main__":
    main()
