import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Query, HTTPException
from pymongo import MongoClient
from botasaurus.browser import browser, Driver
import os
from dotenv import load_dotenv
import uvicorn

load_dotenv() # Loads variables from .env file

MONGO_URI = os.getenv("MONGO_URI")
# Initialize FastAPI
app = FastAPI(title="Udemy Scraper API")

# Initialize MongoDB Connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["udemy_scraper_db"]
cache_collection = db["courses_cache"]

# Cache expiration limit (e.g., 7 days)
CACHE_EXPIRATION_DAYS = 7


# --- BOTASAURUS SCRAPER FUNCTION ---
@browser(
    headless=True,
    block_images=True, # Saves ~150MB of RAM by ignoring images
    reuse_driver=True, # Keeps single Chrome process alive instead of spawning new ones
    add_arguments=[
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--window-size=800,600",                  # Smaller window size saves buffer RAM
        "--js-flags=--max-old-space-size=256"      # Caps Chrome V8 engine RAM at 256MB
    ]
)
def scrape_udemy_interceptor(driver: Driver, data):
    keyword = data.get("keyword")
    max_courses = data.get("max_courses", 3)

    # 1. Google Dork Search
    google_url = f"https://www.google.com/search?q=site:udemy.com/course/+{keyword}"
    driver.get(google_url)
    driver.sleep(2)

    links = driver.select_all('a[href*="udemy.com/course/"]')
    courses = []
    seen = set()

    for link in links:
        href = link.get_attribute("href")
        if not href:
            continue

        clean_url = href.split("?")[0].split("&")[0]
        if "/course/" in clean_url and clean_url not in seen:
            seen.add(clean_url)
            title = link.text.split("\n")[0].replace(" - Udemy", "").strip()
            if title:
                courses.append({"title": title, "url": clean_url, "reviews": []})

        if len(courses) >= max_courses:
            break

    # 2. Intercept Reviews per Course Page
    # 2. Extract reviews by querying Udemy's review endpoint directly using the page's course_id
    for course in courses:
        print(f"Fetching course page: {course['title'][:40]}...")
        driver.get(course["url"])
        driver.sleep(2)

        # STEP A: Extract course_id and fetch reviews directly in Chrome runtime
        reviews_data = driver.run_js("""
            return (async () => {
                try {
                    // 1. Locate the course_id from metadata or page attributes
                    let courseId = document.body.getAttribute('data-clp-course-id');
                    
                    if (!courseId && window.UD && window.UD.config && window.UD.config.course) {
                        courseId = window.UD.config.course.id;
                    }
                    
                    if (!courseId) {
                        const el = document.querySelector('[data-course-id]');
                        if (el) courseId = el.getAttribute('data-course-id');
                    }
                    
                    if (!courseId) {
                        const match = document.documentElement.innerHTML.match(/["']course_id["']\\s*:\\s*(\\d+)/i) ||
                                      document.documentElement.innerHTML.match(/courseId["']?\\s*:\\s*(\\d+)/i);
                        if (match) courseId = match[1];
                    }

                    if (!courseId) return { error: "Course ID not found" };

                    // 2. Query Udemy's reviews endpoint using the extracted courseId
                    const apiUrl = `https://www.udemy.com/api-2.0/courses/${courseId}/reviews/?page=1&page_size=5`;
                    const res = await fetch(apiUrl);
                    
                    if (res.ok) {
                        const json = await res.json();
                        return json.results || [];
                    }
                    return [];
                } catch(err) {
                    return [];
                }
            })();
        """)

        reviews = []
        if isinstance(reviews_data, list):
            for r in reviews_data[:5]:
                user_info = r.get("user", {})
                user_name = user_info.get("display_name") or user_info.get("title") or "Anonymous"
                comment = r.get("content") or r.get("body") or ""
                
                # Clean up HTML tags if present
                clean_comment = comment.replace("<p>", "").replace("</p>", "").strip()
                
                reviews.append({
                    "user": user_name,
                    "rating": r.get("rating"),
                    "comment": clean_comment
                })
            print(f" Found {len(reviews)} reviews for '{course['title'][:30]}'")
        else:
            print(f" Could not extract reviews for '{course['title'][:30]}'")

        course["reviews"] = reviews

    return courses  

# --- API ENDPOINTS ---
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Udemy Scraper API"}


@app.get("/scrape")
def scrape_courses(
    keyword: str = Query(..., description="Keyword to search, e.g. python"),
    max_courses: int = Query(
        3, ge=1, le=10, description="Max courses to return"
    ),
    force_refresh: bool = Query(
        False, description="Bypass cache and force live scrape"
    ),
):
    normalized_keyword = keyword.lower().strip()

    # STEP 1: Check MongoDB Cache
    if not force_refresh:
        cached_item = cache_collection.find_one(
            {"keyword": normalized_keyword}, {"_id": 0}
        )
        if cached_item:
            updated_at = cached_item.get("updated_at")
            # Verify cache isn't stale
            if updated_at and (
                datetime.utcnow() - updated_at
            ) < timedelta(days=CACHE_EXPIRATION_DAYS):
                return {
                    "source": "cache",
                    "keyword": normalized_keyword,
                    "updated_at": updated_at.isoformat(),
                    "total": len(cached_item.get("courses", [])),
                    "data": cached_item.get("courses", []),
                }

    # STEP 2: Live Scrape via Botasaurus (Synchronous endpoints run in FastAPI threadpool)
    try:
        scraped_data = scrape_udemy_interceptor(
            data={"keyword": normalized_keyword, "max_courses": max_courses}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scraping failed: {str(e)}")

    # STEP 3: Save / Update MongoDB Cache
    cache_payload = {
        "keyword": normalized_keyword,
        "courses": scraped_data,
        "updated_at": datetime.utcnow(),
    }

    cache_collection.update_one(
        {"keyword": normalized_keyword}, {"$set": cache_payload}, upsert=True
    )

    return {
        "source": "live_scrape",
        "keyword": normalized_keyword,
        "updated_at": cache_payload["updated_at"].isoformat(),
        "total": len(scraped_data),
        "data": scraped_data,
    }

if __name__ == "__main__":
    
    # Automatically triggers uvicorn when running 'python main.py'
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
