import streamlit as st
import geopandas as gpd
import pandas as pd
import osmnx as ox
from osmnx.features import features_from_polygon
import networkx as nx
from shapely.geometry import Point, Polygon
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import folium
from streamlit_folium import st_folium

# --- Page Setup ---
st.set_page_config(layout="wide")
st.title("OCEAN Demo")

# --- Step 1: Upload CSV ---
uploaded_csv = st.file_uploader("Upload CSV with member data (column: 'address')", type="csv")

df = None
if uploaded_csv:
    df = pd.read_csv(uploaded_csv)
    if 'address' not in df.columns:
        st.error("CSV must have a column named 'address'")
    else:
        st.success(f"Loaded {len(df)} addresses.")

        # Preview uploaded addresses
        st.markdown("### 📄 Uploaded Address Table")
        st.dataframe(df, height=300)

# --- Step 2: Draw Map Polygon ---
st.markdown("Draw Turf")
with st.expander("Click to draw polygon"):
    m = folium.Map(location=[40.7128, -74.006], zoom_start=13)
    draw = folium.plugins.Draw(export=True)
    draw.add_to(m)
    output = st_folium(m, width=700, height=500, returned_objects=["last_active_drawing"])

# --- Step 3: Extract Polygon & Get OSM Buildings ---
polygon = None
buildings_gdf = None

if output and output.get("last_active_drawing"):
    coords = output["last_active_drawing"]["geometry"]["coordinates"][0]
    polygon = Polygon([(pt[0], pt[1]) for pt in coords])
    st.success("Polygon drawn.")

if polygon:
    st.markdown("Downloa Data")
    try:
        tags = {"building": True}
        buildings_gdf = features_from_polygon(polygon, tags)
        buildings_gdf = buildings_gdf[buildings_gdf.geometry.type == 'Polygon']
        st.success(f"Downloaded {len(buildings_gdf)} building footprints.")
    except Exception as e:
        st.error(f"Error pulling OSM data: {e}")

# --- Step 4: Match to CSV + Reverse Geocode ---
if buildings_gdf is not None and df is not None:
    st.markdown("Review")
    geolocator = Nominatim(user_agent="sidewalksort")
    geocode = RateLimiter(geolocator.reverse, min_delay_seconds=1)

    def get_address_from_geom(geom):
        try:
            location = geocode((geom.centroid.y, geom.centroid.x))
            return location.address if location else "Unknown"
        except:
            return "Unknown"

    buildings_gdf["osm_address"] = buildings_gdf["addr:housenumber"].fillna("") + " " + buildings_gdf["addr:street"].fillna("")
    buildings_gdf["osm_address"] = buildings_gdf["osm_address"].str.strip()

    matched, reverse, unknown = [], [], []
    for _, row in buildings_gdf.iterrows():
        addr = row["osm_address"]
        if addr and addr in df["address"].values:
            matched.append(addr)
        elif not addr or addr.strip() == "":
            rev = get_address_from_geom(row.geometry)
            reverse.append(rev)
        else:
            unknown.append(addr)

    st.markdown(f"**Matched:** {len(matched)} &nbsp;&nbsp;|&nbsp;&nbsp; **Reverse-geocoded:** {len(reverse)} &nbsp;&nbsp;|&nbsp;&nbsp; **Unknown:** {len(unknown)}")
    st.markdown("---")

    # Create DataFrame and show result
    route_df = pd.DataFrame({
        "Address": matched + reverse + unknown
    })

    st.markdown("### 📍 Final Address Table (Matched + Reverse + Unknown)")
    st.dataframe(route_df, height=300)

    st.download_button(
        label="📥 Download Ordered Address List as CSV",
        data=route_df.to_csv(index=False),
        file_name="sorted_addresses.csv",
        mime="text/csv"
    )

# --- Footer ---
st.markdown("---")
st.markdown("Demo made for NYPIRG FUND by Lucy Zentgraf, 2025. All Rights Reserved")
