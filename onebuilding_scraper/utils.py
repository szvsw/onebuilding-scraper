"""A module for scraping the climate.onebuilding.org website for EPW files."""

import asyncio
import re
import shutil
from pathlib import Path
from typing import cast

import httpx
from bs4 import BeautifulSoup, Tag
from tqdm.asyncio import tqdm as atqdm


async def get_root(client: httpx.AsyncClient):
    """Retrieves the root elements from the client.

    Args:
        client (httpx.AsyncClient): The HTTP client.

    Returns:
        regions (list[str]): A list of regions extracted from the root elements.
    """
    home = await client.get("/default.html")
    soup = BeautifulSoup(home.content, "html.parser")
    # find all a tags with hrefs that start with "WMO_REGION_"
    regions = list({
        cast(str, a["href"])
        for a in soup.find_all("a", href=re.compile(r"WMO_Region_"))
    })
    return regions


async def get_subregions(url: str, client: httpx.AsyncClient) -> list[str]:
    """Retrieves the subregions from the given URL.

    Args:
        url (str): The URL to scrape for subregions.
        client (httpx.AsyncClient): The HTTP client to use for the request.

    Returns:
        list[str]: A list of subregions found in the URL.
    """
    res = await client.get(url)
    soup = BeautifulSoup(res.content, "html.parser")
    # get any a tags which are in td tags
    tags: list[Tag] = soup.find_all("td")
    regions: list[str] = []
    for tag in tags:
        a = tag.find("a")
        if not isinstance(a, Tag):
            continue
        href = cast(str, a["href"])
        if href.endswith("html"):
            child_region = Path(url).parent / href
            regions.append(child_region.as_posix())

    return regions


async def get_file_list(url: str, client: httpx.AsyncClient) -> list[str]:
    """Retrieves a list of file URLs from a given URL.

    Args:
        url (str): The URL to scrape for file URLs.
        client (httpx.AsyncClient): The HTTP client to use for the request.

    Returns:
        List[str]: A list of file URLs.

    """
    res = await client.get(url)
    soup = BeautifulSoup(res.content, "html.parser")
    # get the table element with class "file-table"
    table = soup.find("table", summary="file table")
    if not isinstance(table, Tag):
        flist: list[str] = []
        return flist
    a_tags = table.find_all("a", href=re.compile(r".*\.zip"))
    urls: list[str] = []
    for tag in a_tags:
        resource_url: Path = Path(url).parent / tag["href"]
        urls.append(resource_url.as_posix())
    return urls


def make_row_dict(path: str):
    """Parses the contents of a file at the given path and returns a dictionary containing relevant information.

    Args:
        path (str): The path to the file.

    Returns:
        metadata (dict[str, int | bool | float]): A dictionary containing the following information:
            - name (str): The name of the file.
            - location (str): The location in the format "POINT(lon lat)".
            - path (str): The path to the file.
            - country (str): The country name.
            - province (str): The province name.
            - city (str): The city name.
            - lat (float): The latitude.
            - lon (float): The longitude.
            - wmo (str): The WMO code.
            - tz (float): The time zone.
            - TM3 (bool): True if the file is TMY3, False otherwise.
            - TMx (bool): True if the file is TMYx, False otherwise.
            - year (int): The year.
            - start_year (int): The start year (if applicable).
            - end_year (int): The end year (if applicable).
    """
    with open(path) as f:
        epw = f.readline()
    data = epw.split(",")
    city = data[1]
    province = data[2]
    country = data[3]
    lat = float(data[-4])
    lon = float(data[-3])
    location = f"POINT({lon} {lat})"
    tz = float(data[-2])
    file_path = Path(path)
    name = file_path.stem
    is_tmy3 = "tmy3" in name.lower()
    is_tmyx = "tmyx" in name.lower()
    wmo_match = re.compile(r".*\.(\d{6})").match(name)
    wmo = wmo_match.group(1) if wmo_match else None
    year_pattern = r"(?<![0-9])(?:20|19)\d{2}(?![0-9])"
    applicable_years = re.findall(year_pattern, name)
    start_year = int(applicable_years[0]) if len(applicable_years) == 2 else None
    end_year = int(applicable_years[1]) if len(applicable_years) == 2 else None
    year = int(applicable_years[0]) if len(applicable_years) == 1 else None

    data = {
        "name": name,
        "location": location,
        "path": path,
        "country": country,
        "province": province,
        "city": city,
        "lat": lat,
        "lon": lon,
        "wmo": wmo,
        "tz": tz,
        "TM3": is_tmy3,
        "TMx": is_tmyx,
        "year": year,
        "start_year": start_year,
        "end_year": end_year,
    }
    return data


async def generate_paths(client: httpx.AsyncClient):
    """Generate a list of file paths.

    Args:
        client (httpx.AsyncClient): An instance of httpx.AsyncClient.

    Returns:
        files (list[str]): A list of file paths.
    """
    regions = await get_root(client)
    subregion_promises = [get_subregions(region, client) for region in regions]
    subregions = [
        r
        for region in cast(
            list[list[str]],
            await atqdm.gather(*subregion_promises),  # pyright: ignore [reportUnknownMemberType]
        )
        for r in region
    ]
    file_promises = [get_file_list(subregion, client) for subregion in subregions]
    files = [
        f
        for subregion in cast(
            list[list[str]],
            await atqdm.gather(*file_promises),  # pyright: ignore [reportUnknownMemberType]
        )
        for f in subregion
    ]
    return files


async def download_zip_and_unzip(
    file: str, output_dir: Path, client: httpx.AsyncClient
):
    """Downloads a zip file from a given URL, extracts its contents, and returns the path to the extracted EPW file.

    Args:
        file (str): The URL of the zip file to download.
        output_dir (Path): The directory where the extracted files will be saved.
        client (httpx.AsyncClient): The HTTP client to use for the request.

    Returns:
        status_code (int): The status code.
        out_epw (Path | Exception): The path to the extracted EPW file or an exception if an error occurred.
    """
    out_zip = output_dir / Path(file)
    out_folder = out_zip.parent / out_zip.stem
    out_epw = out_folder / f"{out_zip.stem}.epw"
    if not (out_epw).exists():
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = await client.get(file)
        except Exception as e:
            return (-1, e)
        try:
            with open(out_zip, "wb") as f:
                f.write(res.content)
            out_folder.mkdir(parents=True, exist_ok=True)
            shutil.unpack_archive(out_zip, out_folder)
            out_zip.unlink()

        except Exception as e:
            out_zip.unlink(missing_ok=True)
            shutil.rmtree(out_folder)
            return (-2, e)
    else:
        await asyncio.sleep(0.01)
    return (0, out_epw)
    # try:
    #     data = make_row_dict(out_epw)
    #     data["file"] = file
    #     return (0, data)
    # except Exception as e:
    #     return (-3, e)
