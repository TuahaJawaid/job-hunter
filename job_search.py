#!/usr/bin/env python3
"""
Job Hunter Agent - Automated Job Search Engine
Searches multiple job platforms for Senior Accountant positions,
stores results, and feeds them to an interactive dashboard.
"""

import json
import os
import hashlib
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
JOBS_DB_PATH = DATA_DIR / "jobs_db.json"
SEARCH_LOG_PATH = DATA_DIR / "search_log.json"

DATA_DIR.mkdir(exist_ok=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_jobs_db():
    if JOBS_DB_PATH.exists():
        with open(JOBS_DB_PATH) as f:
            return json.load(f)
    return {"jobs": [], "last_updated": None, "total_searches": 0}


def save_jobs_db(db):
    db["last_updated"] = datetime.now().isoformat()
    with open(JOBS_DB_PATH, "w") as f:
        json.dump(db, f, indent=2)


def log_search(query, platform, results_count):
    log = []
    if SEARCH_LOG_PATH.exists():
        with open(SEARCH_LOG_PATH) as f:
            log = json.load(f)
    log.append({
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "platform": platform,
        "results_count": results_count
    })
    # Keep last 500 log entries
    log = log[-500:]
    with open(SEARCH_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def generate_job_id(title, company, url):
    raw = f"{title}|{company}|{url}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def parse_search_results(raw_text, platform, query):
    """
    Parse web search results text into structured job listings.
    Uses heuristics to extract job titles, companies, locations, and URLs.
    """
    jobs = []
    lines = raw_text.strip().split("\n")

    current_job = {}
    for line in lines:
        line = line.strip()
        if not line:
            if current_job.get("title"):
                jobs.append(current_job)
                current_job = {}
            continue

        # Try to detect job title lines (usually contain the role keyword)
        title_keywords = ["accountant", "accounting", "finance", "controller", "bookkeeper", "cpa", "ledger", "audit"]
        line_lower = line.lower()

        if any(kw in line_lower for kw in title_keywords) and not current_job.get("title"):
            # Clean up the title
            title = re.sub(r'\[.*?\]', '', line).strip()
            title = re.sub(r'^[-•*]\s*', '', title).strip()
            if len(title) > 10 and len(title) < 200:
                current_job["title"] = title

        # Detect company names (often after "at" or "-")
        if " at " in line_lower and current_job.get("title") and not current_job.get("company"):
            parts = line.split(" at ", 1)
            if len(parts) > 1:
                current_job["company"] = parts[1].strip()[:100]

        # Detect locations
        location_patterns = [
            r'(remote)',
            r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)?,\s*[A-Z]{2})',
            r'(hybrid)',
        ]
        for pattern in location_patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match and not current_job.get("location"):
                current_job["location"] = match.group(1).strip()

        # Detect salary info
        salary_pattern = r'\$[\d,]+(?:\s*-\s*\$[\d,]+)?(?:\s*(?:per|a|/)\s*(?:year|yr|annual))?'
        salary_match = re.search(salary_pattern, line)
        if salary_match and not current_job.get("salary"):
            current_job["salary"] = salary_match.group(0)

        # Detect URLs
        url_match = re.search(r'(https?://[^\s\)]+)', line)
        if url_match and not current_job.get("url"):
            current_job["url"] = url_match.group(1)

    # Don't forget the last job
    if current_job.get("title"):
        jobs.append(current_job)

    return jobs


def build_search_queries(config):
    """Build search queries for different platforms."""
    queries = []
    titles = config["search"]["job_titles"]
    locations = config["search"]["locations"]
    days = config["search"]["posted_within_days"]

    for title in titles:
        for location in locations:
            for platform in config["platforms"]:
                query = f"{title} jobs {location} {platform}.com posted last {days} days"
                queries.append({
                    "query": query,
                    "title": title,
                    "location": location,
                    "platform": platform
                })
    return queries


def is_excluded(job, config):
    """Check if a job should be excluded based on config."""
    exclude = config["search"]["exclude_keywords"]
    title_lower = job.get("title", "").lower()
    for kw in exclude:
        if kw.lower() in title_lower:
            return True
    return False


def enrich_job(job, query_info):
    """Add metadata to a job listing."""
    job.setdefault("company", "Unknown Company")
    job.setdefault("location", query_info.get("location", "Not specified"))
    job.setdefault("salary", "Not listed")
    job.setdefault("url", "")
    job.setdefault("platform", query_info.get("platform", "unknown"))
    job.setdefault("description", "")

    job["id"] = generate_job_id(job["title"], job["company"], job.get("url", ""))
    job["found_date"] = datetime.now().isoformat()
    job["search_query"] = query_info.get("title", "")
    job["status"] = "new"  # new, reviewed, starred, applied, rejected
    job["notes"] = ""

    return job


def search_jobs_web(config):
    """
    Main search function - uses web search to find job listings.
    This is the entry point called by the scheduled task.
    """
    print(f"\n{'='*60}")
    print(f"  JOB HUNTER AGENT - Search Run")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    db = load_jobs_db()
    existing_ids = {j["id"] for j in db["jobs"]}
    new_jobs = []

    queries = build_search_queries(config)
    print(f"Running {len(queries)} search queries across {len(config['platforms'])} platforms...\n")

    for i, q_info in enumerate(queries):
        query = q_info["query"]
        print(f"  [{i+1}/{len(queries)}] Searching: {query[:60]}...")

        # We'll use a subprocess to call the web search via the Claude tool
        # In scheduled mode, this file is executed and results are parsed
        log_search(query, q_info["platform"], 0)

    # Return the queries for the orchestrator to execute
    return queries, db, existing_ids


def process_results(raw_results, queries, db, existing_ids, config):
    """Process raw search results and update the database."""
    new_jobs = []

    for result_text, q_info in zip(raw_results, queries):
        parsed = parse_search_results(result_text, q_info["platform"], q_info["query"])

        for job in parsed:
            if is_excluded(job, config):
                continue

            job = enrich_job(job, q_info)
            if job["id"] not in existing_ids:
                new_jobs.append(job)
                existing_ids.add(job["id"])

    # Add new jobs to database
    db["jobs"].extend(new_jobs)
    db["total_searches"] = db.get("total_searches", 0) + 1

    # Sort by found_date descending
    db["jobs"].sort(key=lambda j: j.get("found_date", ""), reverse=True)

    save_jobs_db(db)

    print(f"\n{'='*60}")
    print(f"  SEARCH COMPLETE")
    print(f"  New jobs found: {len(new_jobs)}")
    print(f"  Total jobs in database: {len(db['jobs'])}")
    print(f"{'='*60}\n")

    return new_jobs


def get_stats(db):
    """Get dashboard statistics."""
    jobs = db.get("jobs", [])
    now = datetime.now()
    today = now.date()

    stats = {
        "total_jobs": len(jobs),
        "new_today": sum(1 for j in jobs if j.get("found_date", "")[:10] == str(today)),
        "starred": sum(1 for j in jobs if j.get("status") == "starred"),
        "applied": sum(1 for j in jobs if j.get("status") == "applied"),
        "reviewed": sum(1 for j in jobs if j.get("status") == "reviewed"),
        "rejected": sum(1 for j in jobs if j.get("status") == "rejected"),
        "total_searches": db.get("total_searches", 0),
        "last_updated": db.get("last_updated", "Never"),
        "platforms": {},
        "by_location": {},
    }

    for j in jobs:
        p = j.get("platform", "unknown")
        stats["platforms"][p] = stats["platforms"].get(p, 0) + 1
        loc = j.get("location", "Unknown")
        stats["by_location"][loc] = stats["by_location"].get(loc, 0) + 1

    return stats


if __name__ == "__main__":
    config = load_config()
    queries, db, existing_ids = search_jobs_web(config)
    print("\nSearch queries prepared. Run via scheduled task for full automation.")
    print(f"Database location: {JOBS_DB_PATH}")
    stats = get_stats(db)
    print(f"Current stats: {json.dumps(stats, indent=2)}")
