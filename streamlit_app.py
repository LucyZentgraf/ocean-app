import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
from shapely.geometry import Point, Polygon
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from streamlit_folium import st_folium

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo")

# --- Step 1: Optional CSV Upload ---
uploaded_csv = st.file_uploader("Upload CSV with member data (column: 'address')", type="csv")
df = None
if uploaded_csv:
    df = pd.read_csv(uploaded_csv)
    if 'address' not in df.columns:
        st.error("CSV must have a column named 'address'")
    else:
        st.success(f"Loaded {len(df)} addresses.")
        st.markdown("Uploaded Member Data")
        st.dataframe(df, height=300)

# --- Step 2: Draw Map Turf ---
st.markdown("Draw Turf")
with st.expander("Click to draw polygon"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = folium.plugins.Draw(export=True)
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["last_active_drawing"])

# --- Step 3: Download OSM Building Data ---
polygon = None
buildings_gdf = None

if output and output.get("last_active_drawing"):
    coords = output["last_active_drawing"]["geometry"]["coordinates"][0]
    polygon = Polygon([(pt[0], pt[1]) for pt in coords])
    st.success("Polygon drawn.")

if polygon:
    st.markdown("Review Data")
    try:
        tags = {"building": True}
        buildings_gdf = features_from_polygon(polygon, tags)
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == 'Polygon']
        buildings_gdf = buildings_gdf[~buildings_gdf["building"].isin([
            "commercial", "industrial", "retail", "garage", "service", "warehouse", "school", "university"
        ])]
        st.success(f"Downloaded {len(buildings_gdf)} filtered residential-like building footprints.")
    except Exception as e:
        st.error(f"Error pulling OSM data: {e}")

# --- Step 4: Extract or Match Addresses ---
if buildings_gdf is not None:
    st.markdown("Addresses")

    # If no buildings found, try fallback to structures
    if buildings_gdf.empty:
        st.warning("No buildings found. Attempting to pull structures as fallback...")
        try:
            tags = {"man_made": "building"}
            buildings_gdf = features_from_polygon(polygon, tags)
            buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == "Polygon"]
            st.success(f"Downloaded {len(buildings_gdf)} structure polygons from OSM.")
        except Exception as e:
            st.error(f"Structure fallback failed: {e}")
            buildings_gdf = None

if buildings_gdf is not None and not buildings_gdf.empty:
    geolocator = Nominatim(user_agent="sidewalksort")
    geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

    # Debugging aid: show OSM tag columns
    st.write("Available OSM columns:", buildings_gdf.columns.tolist())

    # Address construction with fallback
    housenumber_col = "addr:housenumber" if "addr:housenumber" in buildings_gdf.columns else None
    street_col = "addr:street" if "addr:street" in buildings_gdf.columns else None

    if housenumber_col and street_col:
        buildings_gdf["osm_address"] = buildings_gdf[housenumber_col].fillna("") + " " + buildings_gdf[street_col].fillna("")
    elif street_col:
        buildings_gdf["osm_address"] = buildings_gdf[street_col].fillna("")
    elif housenumber_col:
        buildings_gdf["osm_address"] = buildings_gdf[housenumber_col].fillna("")
    else:
        buildings_gdf["osm_address"] = ""

    buildings_gdf["osm_address"] = buildings_gdf["osm_address"].str.strip()

    # Caching results to prevent repeated geocoding
    address_cache = {}

    def safe_reverse_geocode(geom):
        lat, lon = geom.centroid.y, geom.centroid.x
        key = f"{lat:.5f},{lon:.5f}"
        if key in address_cache:
            return address_cache[key]
        try:
            location = geocode((lat, lon))
            address = location.address if location else "Unknown"
            address_cache[key] = address
            return address
        except Exception as e:
            error_msg = f"Reverse geocode error: {e}"
            address_cache[key] = error_msg
            return error_msg

    final_data = []

    for _, row in buildings_gdf.iterrows():
        addr = row.get("osm_address", "").strip()
        source = ""
        note = ""

        if df is not None and addr and addr in df["address"].values:
            source = "OSM Match"
        elif addr:
            rev = safe_reverse_geocode(row.geometry)
            source = "Reverse Geocoded"
            if "error" in rev.lower():
                note = rev
            addr = rev
        else:
            rev = safe_reverse_geocode(row.geometry)
            source = "Reverse Geocoded (fallback)"
            if "error" in rev.lower():
                note = rev
            addr = rev

        final_data.append({"Address": addr, "Source": source, "Note": note})

    final_df = pd.DataFrame(final_data).drop_duplicates()

    # --- Step 5: Routing Placeholder ---
    st.markdown("Route")
    st.info("Routing based on pedestrian-safe sidewalk logic will be implemented in the next version.")

    # --- Step 6: Final Table + Download ---
    st.markdown("Turf Log")
    st.dataframe(final_df, height=400)

    st.download_button(
        label="Download Turf Log",
        data=final_df.to_csv(index=False),
        file_name="ordered_addresses.csv",
        mime="text/csv"
    )

# --- Footer ---
st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")
