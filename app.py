
from flask import (
    Flask, render_template, render_template_string, request, redirect,
    url_for, session, flash, jsonify
)
import os
print("Current working directory:", os.getcwd())
print("Files in directory:", os.listdir('.'))
import uuid
import re
from datetime import datetime
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import json
import sqlite3
try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
    DictCursor = None
import secrets
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import concurrent.futures
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "the-ecosystem-development-key")
if os.environ.get("RENDER") and app.secret_key == "the-ecosystem-development-key":
    app.logger.warning("SECRET_KEY is not configured; set a strong SECRET_KEY in Render Environment.")

# =========================
# FILE UPLOAD SETTINGS
# =========================

UPLOAD_FOLDER = os.path.join("static", "uploads", "businesses")
EVENT_FLYER_FOLDER = os.path.join("static", "uploads", "events")
PREMIUM_MEDIA_FOLDER = os.path.join("static", "uploads", "premium_listings")
SHOWCASE_UPLOAD_FOLDER = os.path.join("static", "uploads", "showcase")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
PREMIUM_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
PREMIUM_VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
MAX_PREMIUM_IMAGE_SIZE = 5 * 1024 * 1024
MAX_PREMIUM_VIDEO_SIZE = 40 * 1024 * 1024
MAX_PHOTO_SIZE = 5 * 1024 * 1024
MAX_EVENT_FLYER_SIZE = 5 * 1024 * 1024

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["EVENT_FLYER_FOLDER"] = EVENT_FLYER_FOLDER
app.config["PREMIUM_MEDIA_FOLDER"] = PREMIUM_MEDIA_FOLDER
app.config["SHOWCASE_UPLOAD_FOLDER"] = SHOWCASE_UPLOAD_FOLDER
# Allows up to 8 images and 3 videos per premium listing while keeping a hard request cap.
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EVENT_FLYER_FOLDER, exist_ok=True)
os.makedirs(PREMIUM_MEDIA_FOLDER, exist_ok=True)
os.makedirs(SHOWCASE_UPLOAD_FOLDER, exist_ok=True)

# =========================
# PERSISTENT APPLICATION STATE
# =========================
# Mutable application records are saved to SQLite after each request.

users = []
business_claims = []
business_submissions = []
business_audit_log = []
admin_activity_log = []  # Audit trail for successful admin actions.
business_notifications = []
event_submissions = []
reviews = []
competition_entries = []
competition_votes = []
reports = []  # User-submitted reports for suspicious businesses and reviews.
showcase_activity = []  # Per-business likes, comments, views and tracked shares from Showcase.
showcase_posts = []  # User-created portfolio posts, persisted in SQLite app state.
marketplace_products = []  # Product listings for THE ECOSYSTEM Marketplace.
marketplace_premium_ad = {}  # Active approved paid premium campaign shown on Marketplace.
spotlight_payments = []
spotlight_campaigns = []
premium_listing_orders = []  # Premium business listing applications and subscriptions.
deleted_items = []  # Recoverable records, separated by ADMIN and USER trash scopes.

# Lightweight system-health telemetry for the admin monitor.
HEALTH_LAST_SAVE_AT = None
HEALTH_LAST_SAVE_ERROR = ""
HEALTH_LAST_EMAIL_ERROR = ""
HEALTH_LAST_EMAIL_SUCCESS_AT = None

ADMIN_EMAIL = "admin@theecosystem.ng"
ADMIN_PASSWORD = "admin123"


# =========================
# FILE AND USER HELPERS
# =========================

def allowed_file(filename):
    return (
        filename
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def get_user_by_id(user_id):
    return next((user for user in users if user["id"] == user_id), None)


def get_business_owner_claim(business_id, user_id):
    return next(
        (
            claim for claim in business_claims
            if claim["user_id"] == user_id
            and claim["business_id"] == business_id
        ),
        None
    )


def get_business_approved_claim(business_id):
    return next(
        (
            claim for claim in business_claims
            if claim["business_id"] == business_id
            and claim.get("status") == "APPROVED"
        ),
        None
    )


def is_business_verified(business_id):
    return get_business_approved_claim(business_id) is not None


def _business_now():
    return datetime.now(timezone.utc).isoformat()


def record_business_audit(action, business_id=None, actor_id=None, details=""):
    """Record important business, ownership and moderation actions."""
    business_audit_log.append({
        "id": max((int(x.get("id", 0)) for x in business_audit_log), default=0) + 1,
        "action": str(action), "business_id": business_id,
        "actor_id": actor_id if actor_id is not None else session.get("user_id", "ADMIN" if session.get("admin_logged_in") else "SYSTEM"),
        "details": str(details)[:1200], "created_at": _business_now()
    })
    if len(business_audit_log) > 5000:
        del business_audit_log[:-5000]


def notify_business_user(user_id, title, message, business_id=None, email=True):
    """Store an in-app notification and attempt email without breaking the main action."""
    user = get_user_by_id(user_id)
    if not user:
        return
    business_notifications.append({
        "id": max((int(x.get("id", 0)) for x in business_notifications), default=0) + 1,
        "user_id": user_id, "business_id": business_id, "title": str(title)[:160],
        "message": str(message)[:1200], "read": False, "created_at": _business_now()
    })
    if email and user.get("email"):
        try:
            send_resend_email(user["email"], title, message)
        except Exception as exc:
            app.logger.warning("Business notification email not sent to %s: %s", user.get("email"), exc)


def notify_admin(title, message, related_type=None, related_id=None):
    """Create an in-app alert for the admin moderator inbox."""
    business_notifications.append({
        "id": max((int(x.get("id", 0)) for x in business_notifications), default=0) + 1,
        "user_id": "ADMIN", "title": str(title)[:160], "message": str(message)[:1200],
        "related_type": related_type, "related_id": related_id,
        "read": False, "created_at": _business_now()
    })


def notify_user_action(user_id, title, message, related_type=None, related_id=None, email=False):
    """User-facing status update, stored in the same notification bell feed."""
    if user_id is None:
        return
    notify_business_user(user_id, title, message, related_id if related_type == "business" else None, email=email)
    item = next((n for n in reversed(business_notifications)
                 if str(n.get("user_id")) == str(user_id) and n.get("title") == str(title)
                 and n.get("message") == str(message)), None)
    if item:
        item["related_type"] = related_type
        item["related_id"] = related_id


def business_has_access(business, user_id):
    """Management access requires approved ownership/manager verification."""
    if not business or user_id is None:
        return False
    uid = str(user_id)
    matching_claims = [c for c in business_claims if str(c.get("business_id")) == str(business.get("id")) and str(c.get("user_id")) == uid]
    if matching_claims:
        return any(c.get("status") == "APPROVED" for c in matching_claims)
    if uid in {str(x) for x in business.get("manager_ids", [])}:
        return True
    # Legacy records created before the evidence-backed claim workflow.
    return str(business.get("submitted_by", "")) == uid and not business.get("primary_owner_id")


def business_management_blocked(business):
    return bool(business and business.get("ownership_dispute"))


# =========================
# BUSINESS DATA
# =========================

businesses = {
    1: {
        "id": 1, "name": "Local Bites", "category": "FOOD",
        "location": "BODIJA · IBADAN",
        "description": "A local food spot serving Nigerian meals and everyday favourites.",
        "about": "Local Bites brings familiar Nigerian meals into one easy-to-discover local food spot. Whether you're looking for a quick meal or somewhere to eat with friends, this is one of the places worth checking out.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    2: {
        "id": 2, "name": "The City Lodge", "category": "HOTELS",
        "location": "RING ROAD · IBADAN",
        "description": "Comfortable accommodation for business, leisure and short stays.",
        "about": "The City Lodge provides comfortable accommodation for visitors, professionals and locals looking for a convenient place to stay around Ibadan.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    3: {
        "id": 3, "name": "House of Style", "category": "FASHION & BEAUTY",
        "location": "BODIJA · IBADAN",
        "description": "Fashion, styling and beauty services from local creatives.",
        "about": "House of Style brings fashion, styling and beauty services together through local creatives who understand personal style and modern looks.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    4: {
        "id": 4, "name": "FixPoint", "category": "SERVICES",
        "location": "MOKOLA · IBADAN",
        "description": "Local repair and technical services for everyday problems.",
        "about": "FixPoint provides practical repair and technical services for everyday problems, helping people get their equipment and belongings working again.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    5: {
        "id": 5, "name": "Market House", "category": "SHOPPING",
        "location": "DUGBE · IBADAN",
        "description": "Discover products, vendors and local brands in one place.",
        "about": "Market House connects shoppers with products, vendors and local brands, making it easier to discover what is available around Ibadan.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    6: {
        "id": 6, "name": "The Hangout", "category": "ENTERTAINMENT",
        "location": "SAMONDA · IBADAN",
        "description": "A social spot for music, games, food and good times.",
        "about": "The Hangout is a social destination built around music, games, food and spending time with friends.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    7: {
        "id": 7, "name": "Tech Hub Ibadan", "category": "TECH",
        "location": "SAMONDA · IBADAN",
        "description": "A community around technology, startups and digital innovation.",
        "about": "Tech Hub Ibadan brings together people interested in technology, startups, digital products and innovation.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    8: {
        "id": 8, "name": "The Grill Spot", "category": "FOOD",
        "location": "AKOBO · IBADAN",
        "description": "Grills, quick meals and local favourites for food lovers.",
        "about": "The Grill Spot serves grilled food and quick meals for people looking for something tasty around Akobo.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    },
    9: {
        "id": 9, "name": "Fresh Cuts", "category": "FASHION & BEAUTY",
        "location": "BODIJA · IBADAN",
        "description": "Modern grooming and barbering for people who care about their look.",
        "about": "Fresh Cuts focuses on modern grooming and barbering for people who want a clean and confident look.",
        "phone": "", "whatsapp": "", "instagram": "", "website": "",
        "opening_hours": "", "photos": []
    }
}


# =========================
# REVIEWS AND RATINGS
# =========================

def get_business_reviews(business_id):
    return [review for review in reviews if str(review.get("business_id")) == str(business_id) and str(review.get("status", "APPROVED")).upper() != "HIDDEN"]


def get_business_engagement_metrics(business_id):
    """Aggregate persisted Showcase activity for a single business."""
    activity = [item for item in showcase_activity if str(item.get("business_id")) == str(business_id)]
    comments = [item for item in activity if str(item.get("action", "")).lower() == "comment" and not item.get("hidden")]
    return {
        "showcase_likes": sum(1 for item in activity if str(item.get("action", "")).lower() == "like" and not item.get("removed")),
        "showcase_comments": len(comments),
        "showcase_views": sum(1 for item in activity if str(item.get("action", "")).lower() == "view"),
        "showcase_shares": sum(1 for item in activity if str(item.get("action", "")).lower() == "share"),
        "showcase_unanswered": sum(1 for item in comments if not item.get("reply")),
    }


def get_user_business_review(business_id, user_id):
    return next(
        (
            review for review in reviews
            if review["business_id"] == business_id
            and review["user_id"] == user_id
        ),
        None
    )


def get_business_rating(business_id):
    business_reviews = get_business_reviews(business_id)

    if not business_reviews:
        return {"average": 0, "count": 0}

    average = sum(review["rating"] for review in business_reviews) / len(business_reviews)
    return {"average": round(average, 1), "count": len(business_reviews)}


# =========================
# BUSINESS OWNER HELPERS
# =========================

def get_business_submission(business_id, user_id=None):
    return next(
        (
            submission for submission in business_submissions
            if submission.get("business_id") == business_id
            and (user_id is None or submission.get("user_id") == user_id)
        ),
        None
    )


def is_public_business(business):
    """Return True only when a listing is approved for public discovery."""
    status = str(business.get("status", "")).strip().upper()
    hidden_statuses = {
        "PENDING", "PENDING REVIEW", "PENDING_REVIEW", "REJECTED", "HIDDEN", "ARCHIVED", "DISPUTED"
    }

    if status in hidden_statuses:
        return False

    submission = get_business_submission(business.get("id"))
    if submission and str(submission.get("status", "")).strip().upper() != "APPROVED":
        return False

    return True


def can_view_business(business):
    """Public listings are visible to everyone; hidden listings to their owner/admin."""
    if is_public_business(business):
        return True

    current_user_id = session.get("user_id")
    is_owner = business_has_access(business, current_user_id)
    is_admin = session.get("admin_logged_in") is True
    return is_owner or is_admin


def get_user_owned_businesses(user_id):
    owned_businesses = []
    for business in businesses.values():
        uid = str(user_id)
        is_primary = str(business.get("primary_owner_id", business.get("submitted_by", ""))) == uid
        is_manager = uid in {str(x) for x in business.get("manager_ids", [])}
        has_approved_claim = any(str(c.get("business_id")) == str(business.get("id")) and str(c.get("user_id")) == uid and c.get("status") == "APPROVED" for c in business_claims)
        if not (is_primary or is_manager or has_approved_claim):
            continue
        claim = get_business_owner_claim(business["id"], user_id)
        submission = get_business_submission(business["id"], user_id)
        rating = get_business_rating(business["id"])
        status = str(business.get("status", "APPROVED")).upper()
        if business.get("ownership_dispute"):
            status = "DISPUTED"
        elif business.get("archived") or status == "ARCHIVED":
            status = "ARCHIVED"
        elif submission and str(submission.get("status", "")).upper() in {"PENDING", "PENDING REVIEW", "REJECTED"}:
            status = str(submission.get("status")).upper()
        owned_businesses.append({
            "business": business, "claim": claim, "submission": submission,
            "status": status, "rating": rating,
            "is_verified": is_business_verified(business["id"]),
            "review_count": rating["count"], "photo_count": len(business.get("photos", [])),
            "role": "PRIMARY OWNER" if str(business.get("primary_owner_id", business.get("submitted_by", ""))) == str(user_id) else "MANAGER"
        })
    return sorted(owned_businesses, key=lambda item: (item["status"] not in {"PENDING", "PENDING REVIEW"}, item["business"].get("name", "").lower()))


# =========================
# BUSINESS DISCOVERY HELPERS
# =========================

def get_business_profile_picture_url(business):
    """Return only the dedicated profile picture URL, without a gallery fallback."""
    picture = business.get("profile_picture")
    if not isinstance(picture, str) or not picture.strip():
        return ""
    picture = picture.strip()
    if picture.startswith(("http://", "https://", "/", "data:")):
        return picture
    picture = picture.replace("\\", "/").lstrip("./")
    if picture.startswith("static/"):
        return "/" + picture
    if picture.startswith("uploads/"):
        return "/static/" + picture
    if "/" not in picture:
        return "/static/uploads/businesses/" + picture
    return ""


def get_business_card_image(business):
    """Return the dedicated public profile image, falling back to a gallery photo."""
    profile_picture = business.get("profile_picture")
    if isinstance(profile_picture, str) and profile_picture.strip():
        picture = profile_picture.strip()
        if picture.startswith(("http://", "https://", "/", "data:")):
            return picture
        picture = picture.replace("\\", "/").lstrip("./")
        if picture.startswith("static/"):
            return "/" + picture
        if picture.startswith("uploads/"):
            return "/static/" + picture
        if "/" not in picture:
            return "/static/uploads/businesses/" + picture
    photos = business.get("photos") or []
    if isinstance(photos, str):
        photos = [photos]
    for photo in photos:
        if isinstance(photo, dict):
            photo = photo.get("url") or photo.get("path") or photo.get("src")
        if not isinstance(photo, str):
            continue
        photo = photo.strip()
        if not photo:
            continue
        # Uploaded business photos are stored as Flask static URLs. Support
        # legacy relative paths too, without rewriting full URLs or data URLs.
        if photo.startswith(("http://", "https://", "/", "data:")):
            return photo
        photo = photo.replace("\\", "/").lstrip("./")
        if photo.startswith("static/"):
            return "/" + photo
        if photo.startswith("uploads/"):
            return "/static/" + photo
        if "/" not in photo:
            return "/static/uploads/businesses/" + photo
    return ""



@app.template_filter("business_card_image")
def business_card_image_filter(business):
    """Use the current public profile picture first, then a gallery photo."""
    if not isinstance(business, dict):
        return ""
    return get_business_card_image(business)


def get_business_discovery_data():
    discovery_businesses = []

    for business in businesses.values():
        if not is_public_business(business):
            continue

        rating = get_business_rating(business["id"])
        business_copy = business.copy()
        # Show one consistent label on cards even for older saved category names.
        business_copy["category"] = canonical_business_category(business.get("category", ""))
        business_copy["rating_average"] = rating["average"]
        business_copy["rating_count"] = rating["count"]
        business_copy["is_verified"] = is_business_verified(business["id"])
        business_copy["card_image"] = get_business_card_image(business)

        if rating["count"] > 0:
            business_copy["rating_display"] = f"{rating['average']:.1f}"
            business_copy["rating_stars"] = (
                "★" * int(rating["average"])
                + "☆" * (5 - int(rating["average"]))
            )
        else:
            business_copy["rating_display"] = "NEW"
            business_copy["rating_stars"] = "☆☆☆☆☆"

        discovery_businesses.append(business_copy)

    return discovery_businesses


def canonical_business_category(category):
    """Collapse legacy category labels into one consistent public category."""
    raw = str(category or "").strip()
    normalized = re.sub(r"\\s+", " ", raw.casefold())
    normalized = normalized.replace("＆", "&")

    # Food-related legacy labels must all behave as the same category.
    food_aliases = {
        "food", "foods", "food & drink", "food and drink",
        "food & drinks", "food and drinks", "food & beverage",
        "food and beverage", "food & beverages", "food and beverages",
        "food & dining", "food and dining", "food/drinks",
        "food / drinks", "food & refreshments"
    }
    if normalized in food_aliases:
        return "FOOD & DRINKS"

    if normalized in {"fashion", "fashion & beauty", "fashion and beauty"}:
        return "FASHION & BEAUTY"

    return raw.upper()


def get_business_categories():
    """Return unique canonical category names, never duplicate legacy aliases."""
    return sorted({
        canonical_business_category(business.get("category", ""))
        for business in businesses.values()
        if is_public_business(business)
        and str(business.get("category", "")).strip()
    }, key=str.casefold)


def category_slug(category):
    canonical = canonical_business_category(category)
    normalized = canonical.casefold().strip()

    if normalized == "fashion & beauty":
        return "fashion"
    if normalized == "food & drinks":
        return "food-and-drinks"

    normalized = normalized.replace("&", "and")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    return normalized.strip("-")


def business_matches_category(business, selected_category):
    if selected_category in {"", "all"}:
        return True

    return category_slug(business.get("category", "").strip()) == selected_category


def get_rating_label(business):
    count = business.get("rating_count", 0)
    average = business.get("rating_average", 0)

    if count == 0:
        return "NEW · NO REVIEWS"

    review_word = "REVIEW" if count == 1 else "REVIEWS"
    return f"{average:.1f} · {count} {review_word}"


# =========================
# P9 — COMPETITIONS DATA
# =========================

competitions_data = [
    {"id": 1, "title": "Best Restaurant & Food Spot", "category": "FOOD & DINING", "icon": "🍽️", "description": "Celebrating restaurants and food spots people love.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising outstanding restaurants and food spots for flavour, service, value and memorable dining experiences."},
    {"id": 2, "title": "Best Hotel & Hospitality", "category": "HOTELS & HOSPITALITY", "icon": "🏨", "description": "Recognising excellent places to stay and hospitality experiences.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Celebrating hotels, guest houses, resorts and accommodation providers that deliver comfort and quality service."},
    {"id": 3, "title": "Best Fashion & Clothing Brand", "category": "FASHION", "icon": "👗", "description": "Spotlighting fashion brands shaping local style.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising clothing labels, designers and fashion businesses bringing originality and quality to their communities."},
    {"id": 4, "title": "Best Beauty, Barbershop & Salon", "category": "BEAUTY & PERSONAL CARE", "icon": "💈", "description": "Celebrating grooming, beauty and personal-care professionals.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising barbershops, salons, makeup artists, spas and beauty professionals for skill and customer experience."},
    {"id": 5, "title": "Best Supermarket & Retail Store", "category": "SHOPPING & RETAIL", "icon": "🛍️", "description": "Recognising the stores and retailers people rely on.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Celebrating supermarkets, neighbourhood shops, specialist retailers and stores known for service and dependable products."},
    {"id": 6, "title": "Best Real Estate & Property Company", "category": "PROPERTY", "icon": "🏢", "description": "Spotlighting trusted property and real-estate businesses.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising real-estate agencies, property developers and property-service companies contributing to their local market."},
    {"id": 7, "title": "Best Healthcare Facility & Pharmacy", "category": "HEALTH & WELLNESS", "icon": "🏥", "description": "Celebrating healthcare providers serving their communities.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising eligible hospitals, clinics, pharmacies, diagnostic centres and wellness facilities for service and community value."},
    {"id": 8, "title": "Best School & Educational Institution", "category": "EDUCATION", "icon": "🎓", "description": "Recognising institutions helping people learn and grow.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Celebrating schools, training centres and educational institutions supporting learning, skills and opportunity."},
    {"id": 9, "title": "Best Transport & Logistics Company", "category": "TRANSPORT & LOGISTICS", "icon": "🚚", "description": "Spotlighting transport, delivery and logistics services.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising transport operators, dispatch services, delivery companies, car rentals and logistics providers."},
    {"id": 10, "title": "Best Technology Startup & Innovation", "category": "TECHNOLOGY", "icon": "🚀", "description": "Celebrating startups and innovators solving real problems.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising technology startups, digital products and practical innovations creating value in their state."},
    {"id": 11, "title": "Best Agriculture & Manufacturing Business", "category": "INDUSTRY & AGRICULTURE", "icon": "🏭", "description": "Recognising local producers, farms and manufacturers.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Celebrating farms, agribusinesses, food producers and manufacturers building local capacity and jobs."},
    {"id": 12, "title": "Best Events, Lounge & Entertainment Venue", "category": "EVENTS & SOCIAL LIFE", "icon": "🎉", "description": "Spotlighting venues and organisers behind memorable experiences.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising event centres, planners, lounges, clubs and entertainment venues that bring people together."},
    {"id": 13, "title": "Best Home Services & Skilled Professional", "category": "HOME & PROFESSIONAL SERVICES", "icon": "🛠️", "description": "Celebrating reliable tradespeople and everyday service providers.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising skilled professionals and services such as mechanics, electricians, plumbers, tailors, cleaners and repairers."},
    {"id": 14, "title": "Best Content Creator & Media Personality", "category": "MEDIA & CREATIVITY", "icon": "🎥", "description": "Celebrating creators and media personalities making an impact.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "Recognising content creators, presenters, podcasters and media personalities whose work engages and inspires their communities."},
    {"id": 15, "title": "People's Choice Award", "category": "COMMUNITY CHOICE", "icon": "👑", "description": "The community chooses a worthy local favourite.", "status": "COMING SOON", "voting_status": "CLOSED", "about": "A community-voted award celebrating an eligible person, business or place that residents believe deserves recognition."},
]

# Preserve compatibility with older records while ensuring every configured category exists.
for competition in competitions_data:
    competition.setdefault("voting_status", "CLOSED")


def get_competitions_for_state(state_name):
    """Return state-specific competition cards without mutating saved global data."""
    state_name = state_name or "Oyo"
    display_state = "Abuja" if normalize_location(state_name) == "federal capital territory" else state_name

    category_copy = {
        1: ("Celebrating restaurants and food spots in {state} that people return to for flavour, service and value.", "Recognising standout restaurants and food spots across {state} for memorable dining experiences."),
        2: ("Recognising hotels and hospitality businesses in {state} that make every stay count.", "Celebrating hotels, guest houses, resorts and accommodation providers delivering comfort and quality service across {state}."),
        3: ("Spotlighting fashion and clothing brands shaping the style of {state}.", "Recognising local clothing labels, designers and fashion businesses bringing originality and quality to {state}."),
        4: ("Celebrating barbershops, salons and beauty professionals across {state}.", "Recognising grooming and beauty businesses in {state} for their skill, service and customer experience."),
        5: ("Recognising the supermarkets and retail stores people rely on in {state}.", "Celebrating retailers across {state} known for dependable products, good service and a strong customer experience."),
        6: ("Spotlighting property and real-estate businesses shaping {state}.", "Recognising real-estate agencies, developers and property-service companies contributing to {state}."),
        7: ("Celebrating healthcare facilities and pharmacies serving people in {state}.", "Recognising eligible healthcare providers across {state} for service, access and community value."),
        8: ("Recognising schools and educational institutions helping people in {state} learn and grow.", "Celebrating education providers across {state} that support learning, skills and opportunity."),
        9: ("Spotlighting transport, dispatch and logistics services operating across {state}.", "Recognising transport operators and logistics providers helping people and goods move around {state}."),
        10: ("Celebrating startups and innovators solving problems in {state}.", "Recognising technology startups, digital products and practical innovations creating value across {state}."),
        11: ("Recognising farms, producers and manufacturers contributing to {state}.", "Celebrating agricultural and manufacturing businesses building local capacity, products and jobs in {state}."),
        12: ("Spotlighting the event venues and social spaces bringing people together in {state}.", "Recognising event centres, planners, lounges, clubs and entertainment venues across {state}."),
        13: ("Celebrating skilled professionals and everyday service providers across {state}.", "Recognising reliable tradespeople and service businesses such as mechanics, electricians, plumbers, tailors, cleaners and repairers in {state}."),
        14: ("Celebrating creators and media personalities telling the stories of {state}.", "Recognising creators, presenters, podcasters and media personalities whose work engages their communities in {state}."),
        15: ("The people of {state} choose a local favourite deserving recognition.", "A community-voted award where residents of {state} choose an eligible person, business or place deserving the spotlight."),
    }

    state_competitions = []
    for competition in competitions_data:
        card = competition.copy()
        short_description, long_about = category_copy.get(
            card.get("id"), (card.get("description", ""), card.get("about", ""))
        )
        card["title"] = f"{card.get('title', 'Community Award')} in {display_state}" if card.get("id") != 15 else f"People's Choice Award in {display_state}"
        card["description"] = short_description.format(state=display_state)
        card["about"] = long_about.format(state=display_state)
        card["selected_state"] = display_state
        state_competitions.append(card)
    return state_competitions



# =========================
# P9 — COMPETITION HELPERS
# =========================

def get_competition_by_id(competition_id):
    return next(
        (item for item in competitions_data if item["id"] == competition_id),
        None
    )


def get_competition_entry_by_id(entry_id):
    return next(
        (item for item in competition_entries if item["id"] == entry_id),
        None
    )


def get_user_competition_entries(competition_id, user_id):
    return [
        item for item in competition_entries
        if item["competition_id"] == competition_id
        and item["user_id"] == user_id
    ]


def get_competition_vote_count(entry_id):
    return sum(
        1 for vote in competition_votes
        if vote["entry_id"] == entry_id
    )


def get_user_competition_vote(competition_id, user_id):
    return next(
        (
            vote for vote in competition_votes
            if vote["competition_id"] == competition_id
            and vote["user_id"] == user_id
        ),
        None
    )


def get_approved_competition_entries(competition_id):
    """Return all approved nominees for admin/global counts."""
    approved = []

    for entry in competition_entries:
        if (
            entry["competition_id"] == competition_id
            and entry.get("status") == "APPROVED"
        ):
            entry_copy = entry.copy()
            entry_copy["votes"] = get_competition_vote_count(entry["id"])
            approved.append(entry_copy)

    return sorted(approved, key=lambda item: item["votes"], reverse=True)


def get_competition_entry_state(entry):
    """Resolve a nominee's state, including older entries created before state was stored."""
    saved_state = entry.get("state") or entry.get("state_of_residence")
    if saved_state:
        normalized = normalize_location(saved_state)
        if normalized in {"abuja", "abj", "fct", "federal capital", "federal capital territory"}:
            return "Federal Capital Territory"
        return next(
            (state for state in NIGERIA_STATES if normalize_location(state) == normalized),
            str(saved_state).strip(),
        )

    # For older submissions, use the submitter's registered state as a safe fallback.
    submitter = get_user_by_id(entry.get("user_id"))
    if submitter:
        return get_user_home_state(submitter)

    # Legacy linked-business nominations can be assigned using the business location.
    linked_business = businesses.get(entry.get("business_id")) if entry.get("business_id") else None
    if linked_business:
        business_state = get_business_state(linked_business)
        return next(
            (state for state in NIGERIA_STATES if normalize_location(state) == business_state),
            "",
        )
    return ""


def get_approved_competition_entries_for_state(competition_id, state):
    """Return approved nominees only from the requested state."""
    target_state = normalize_location(state)
    return [
        entry for entry in get_approved_competition_entries(competition_id)
        if normalize_location(get_competition_entry_state(entry)) == target_state
    ]


# =========================
# INTRO / WELCOME / HOME
# =========================

@app.route("/")
def intro():
    return render_template("intro.html")


@app.route("/welcome")
def welcome():
    return render_template("welcome.html")




# =========================
# HOMEPAGE — APPROVED PREMIUM BUSINESS CAROUSEL
# =========================

def _parse_campaign_datetime(value):
    """Parse stored campaign timestamps safely; naive timestamps are treated as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def get_homepage_premium_businesses():
    """
    Build carousel slides only from public, approved businesses with a paid,
    approved/active premium listing whose campaign has not expired.
    Supports both legacy single-business orders and multi-item campaign orders.
    """
    now = datetime.now(timezone.utc)
    premium_business_ids = set()

    for order in premium_listing_orders:
        status = str(order.get("status", "")).strip().upper().replace(" ", "_")
        payment_status = str(order.get("payment_status", "")).strip().upper()
        if status not in {"ACTIVE", "APPROVED"} or payment_status not in {
            "SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED", "VERIFIED"
        }:
            continue

        expiry = _parse_campaign_datetime(order.get("expires_at"))
        if expiry is None or expiry <= now:
            continue

        if order.get("campaign_type") == "multi_item":
            for campaign_item in order.get("campaign_items", []):
                if str(campaign_item.get("kind", "")).lower() != "business":
                    continue
                try:
                    premium_business_ids.add(int(campaign_item.get("id")))
                except (TypeError, ValueError):
                    continue
            continue

        # Legacy premium listing orders become tied to a business when the
        # associated submission is approved by the moderator.
        for submission in business_submissions:
            if str(submission.get("premium_listing_order_id", "")) != str(order.get("id")):
                continue
            if str(submission.get("status", "")).strip().upper() != "APPROVED":
                continue
            try:
                premium_business_ids.add(int(submission.get("business_id")))
            except (TypeError, ValueError):
                continue

    slides = []
    for business_id in premium_business_ids:
        business = businesses.get(business_id)
        if not business or not is_public_business(business):
            continue
        if business.get("archived") or str(business.get("status", "APPROVED")).upper() in {
            "DELETED", "ARCHIVED", "REJECTED", "HIDDEN", "DISPUTED", "INACTIVE"
        }:
            continue

        # Also require the business-level premium subscription to be active
        # when these fields exist; older multi-item records are covered by order.
        premium_status = str(business.get("premium_status", "ACTIVE")).strip().upper()
        if premium_status not in {"ACTIVE", "APPROVED"}:
            continue
        business_expiry = _parse_campaign_datetime(business.get("premium_expires_at"))
        if business_expiry is not None and business_expiry <= now:
            continue

        image_url = get_business_card_image(business)
        if not image_url:
            image_url = get_business_profile_picture_url(business)
        if not image_url:
            continue

        slides.append({
            "id": business_id,
            "name": business.get("name", "Business"),
            "category": business.get("category", "Business"),
            "location": business.get("location") or business.get("city") or business.get("state") or "Nigeria",
            "description": business.get("description", ""),
            "image_url": image_url,
            "profile_url": url_for("business_profile", business_id=business_id),
        })

    slides.sort(key=lambda item: item["name"].casefold())
    return slides


@app.route("/home")
def home():
    _expire_spotlight_campaigns()
    premium_businesses = get_homepage_premium_businesses()
    return render_template("home.html", premium_businesses=premium_businesses)


@app.route("/discover")
def discover():
    # An explicit state wins; otherwise use the logged-in user's saved residence.
    requested_slug = request.args.get("state", "").strip().lower()
    requested_state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == requested_slug),
        None,
    ) if requested_slug else None
    default_state = requested_state or get_user_home_state()
    default_slug = make_state_slug(default_state)
    state_businesses = [
        business for business in get_business_discovery_data()
        if business_visible_in_state(business, default_state)
    ]
    return render_template(
        "discover.html",
        state=default_state,
        state_slug=default_slug,
        state_discover=True,
        trending_businesses=state_businesses,
        state_explore_url=url_for("explore_state", state_slug=default_slug),
        state_discover_url=url_for("discover_state", state_slug=default_slug),
    )


@app.route("/discover/state/<state_slug>")
def discover_state(state_slug):
    """State-aware discovery landing page opened after selecting a state on the Nigeria map."""
    state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == state_slug.lower()),
        None
    )
    if state is None:
        return "State not found", 404

    canonical_slug = make_state_slug(state)
    state_businesses = [
        business for business in get_business_discovery_data()
        if business_visible_in_state(business, state)
    ]
    return render_template(
        "discover.html",
        state=state,
        state_slug=canonical_slug,
        state_discover=True,
        trending_businesses=state_businesses,
        state_explore_url=url_for("explore_state", state_slug=canonical_slug),
        state_discover_url=url_for("discover_state", state_slug=canonical_slug),
    )


@app.route("/local")
def local():
    # Respect an explicitly selected state; otherwise use the logged-in user's residence.
    requested_slug = request.args.get("state", "").strip().lower()
    requested_state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == requested_slug),
        None,
    ) if requested_slug else None
    home_state = requested_state or get_user_home_state()
    return render_template(
        "local.html",
        home_state=home_state,
        state_slug=make_state_slug(home_state),
        nigeria_states=NIGERIA_STATES,
    )



# =========================
# NIGERIA STATE & CITY EXPLORATION
# =========================

NIGERIA_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi",
    "Bayelsa", "Benue", "Borno", "Cross River", "Delta",
    "Ebonyi", "Edo", "Ekiti", "Enugu", "Gombe", "Imo",
    "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi",
    "Kogi", "Kwara", "Lagos", "Nasarawa", "Niger",
    "Ogun", "Ondo", "Osun", "Oyo", "Plateau", "Rivers",
    "Sokoto", "Taraba", "Yobe", "Zamfara",
    "Federal Capital Territory"
]

STATE_CITIES = {
    "Abia": [
        {"name": "Umuahia", "slug": "umuahia"},
        {"name": "Aba", "slug": "aba"}
    ],
    "Adamawa": [
        {"name": "Yola", "slug": "yola"},
        {"name": "Mubi", "slug": "mubi"}
    ],
    "Akwa Ibom": [
        {"name": "Uyo", "slug": "uyo"},
        {"name": "Eket", "slug": "eket"}
    ],
    "Anambra": [
        {"name": "Awka", "slug": "awka"},
        {"name": "Onitsha", "slug": "onitsha"},
        {"name": "Nnewi", "slug": "nnewi"}
    ],
    "Bauchi": [
        {"name": "Bauchi", "slug": "bauchi"},
        {"name": "Azare", "slug": "azare"}
    ],
    "Bayelsa": [
        {"name": "Yenagoa", "slug": "yenagoa"}
    ],
    "Benue": [
        {"name": "Makurdi", "slug": "makurdi"},
        {"name": "Gboko", "slug": "gboko"}
    ],
    "Borno": [
        {"name": "Maiduguri", "slug": "maiduguri"}
    ],
    "Cross River": [
        {"name": "Calabar", "slug": "calabar"},
        {"name": "Ogoja", "slug": "ogoja"}
    ],
    "Delta": [
        {"name": "Asaba", "slug": "asaba"},
        {"name": "Warri", "slug": "warri"},
        {"name": "Sapele", "slug": "sapele"}
    ],
    "Ebonyi": [
        {"name": "Abakaliki", "slug": "abakaliki"}
    ],
    "Edo": [
        {"name": "Benin City", "slug": "benin-city"},
        {"name": "Auchi", "slug": "auchi"}
    ],
    "Ekiti": [
        {"name": "Ado-Ekiti", "slug": "ado-ekiti"}
    ],
    "Enugu": [
        {"name": "Enugu", "slug": "enugu"},
        {"name": "Nsukka", "slug": "nsukka"}
    ],
    "Gombe": [
        {"name": "Gombe", "slug": "gombe"}
    ],
    "Imo": [
        {"name": "Owerri", "slug": "owerri"},
        {"name": "Orlu", "slug": "orlu"}
    ],
    "Jigawa": [
        {"name": "Dutse", "slug": "dutse"},
        {"name": "Hadejia", "slug": "hadejia"}
    ],
    "Kaduna": [
        {"name": "Kaduna", "slug": "kaduna"},
        {"name": "Zaria", "slug": "zaria"}
    ],
    "Kano": [
        {"name": "Kano", "slug": "kano"}
    ],
    "Katsina": [
        {"name": "Katsina", "slug": "katsina"},
        {"name": "Daura", "slug": "daura"}
    ],
    "Kebbi": [
        {"name": "Birnin Kebbi", "slug": "birnin-kebbi"},
        {"name": "Argungu", "slug": "argungu"}
    ],
    "Kogi": [
        {"name": "Lokoja", "slug": "lokoja"},
        {"name": "Okene", "slug": "okene"}
    ],
    "Kwara": [
        {"name": "Ilorin", "slug": "ilorin"},
        {"name": "Offa", "slug": "offa"}
    ],
    "Lagos": [
        {"name": "Ikeja", "slug": "ikeja"},
        {"name": "Lagos Island", "slug": "lagos-island"},
        {"name": "Lekki", "slug": "lekki"},
        {"name": "Epe", "slug": "epe"}
    ],
    "Nasarawa": [
        {"name": "Lafia", "slug": "lafia"},
        {"name": "Keffi", "slug": "keffi"}
    ],
    "Niger": [
        {"name": "Minna", "slug": "minna"},
        {"name": "Bida", "slug": "bida"}
    ],
    "Ogun": [
        {"name": "Abeokuta", "slug": "abeokuta"},
        {"name": "Ijebu-Ode", "slug": "ijebu-ode"},
        {"name": "Sagamu", "slug": "sagamu"}
    ],
    "Ondo": [
        {"name": "Akure", "slug": "akure"},
        {"name": "Ondo", "slug": "ondo"},
        {"name": "Owo", "slug": "owo"}
    ],
    "Osun": [
        {"name": "Osogbo", "slug": "osogbo"},
        {"name": "Ile-Ife", "slug": "ile-ife"},
        {"name": "Ilesa", "slug": "ilesa"}
    ],
    "Oyo": [
        {"name": "Ibadan", "slug": "ibadan"},
        {"name": "Ogbomoso", "slug": "ogbomoso"},
        {"name": "Oyo", "slug": "oyo"}
    ],
    "Plateau": [
        {"name": "Jos", "slug": "jos"},
        {"name": "Bukuru", "slug": "bukuru"}
    ],
    "Rivers": [
        {"name": "Port Harcourt", "slug": "port-harcourt"},
        {"name": "Bonny", "slug": "bonny"}
    ],
    "Sokoto": [
        {"name": "Sokoto", "slug": "sokoto"}
    ],
    "Taraba": [
        {"name": "Jalingo", "slug": "jalingo"},
        {"name": "Wukari", "slug": "wukari"}
    ],
    "Yobe": [
        {"name": "Damaturu", "slug": "damaturu"},
        {"name": "Potiskum", "slug": "potiskum"}
    ],
    "Zamfara": [
        {"name": "Gusau", "slug": "gusau"}
    ],
    "Federal Capital Territory": [
        {"name": "Abuja", "slug": "abuja"},
        {"name": "Gwagwalada", "slug": "gwagwalada"},
        {"name": "Kubwa", "slug": "kubwa"}
    ]
}


# =========================
# NIGERIA LOCATION DATASET
# =========================
# The current dataset is stored at static/data/nigeria-lga-clean.json.
# Other common locations are retained as fallbacks for compatibility.
PROJECT_FOLDER = os.path.dirname(os.path.abspath(__file__))
LOCATION_DATA_CANDIDATES = [
    os.path.join(PROJECT_FOLDER, "static", "data", "nigeria-lga-clean.json"),
    os.path.join(PROJECT_FOLDER, "nigeria-lga-clean.json"),
    os.path.join(PROJECT_FOLDER, "data", "nigeria-lga-clean.json"),
    os.path.join(PROJECT_FOLDER, "static", "data", "all-lga.json"),
    os.path.join(PROJECT_FOLDER, "data", "all-lga.json"),
    os.path.join(PROJECT_FOLDER, "all-lga.json"),
    os.path.join(PROJECT_FOLDER, "static", "data", "nigeria_locations.json"),
    os.path.join(PROJECT_FOLDER, "data", "nigeria_locations.json"),
    os.path.join(PROJECT_FOLDER, "nigeria_locations.json"),
]


def normalize_location(value):
    """Normalize location strings for reliable matching across the app."""
    value = str(value or "").strip().lower()
    value = value.replace("·", " ").replace(",", " ")
    value = re.sub(r"[^a-z0-9\s-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _find_owned_business_for_premium_order(order):
    """Return this user's existing business record, so a premium upgrade never duplicates it."""
    owner_id = str(order.get("user_id", ""))
    target_name = normalize_location(order.get("business_name", ""))
    if not owner_id or not target_name:
        return None
    for business in businesses.values():
        if business.get("archived") or str(business.get("status", "")).upper() in {"DELETED", "ARCHIVED"}:
            continue
        if normalize_location(business.get("name", "")) != target_name:
            continue
        primary_owner = str(business.get("primary_owner_id", business.get("submitted_by", "")))
        manager_ids = {str(x) for x in business.get("manager_ids", [])}
        approved_claim = any(
            str(claim.get("business_id")) == str(business.get("id"))
            and str(claim.get("user_id")) == owner_id
            and str(claim.get("status", "")).upper() == "APPROVED"
            for claim in business_claims
        )
        if primary_owner == owner_id or owner_id in manager_ids or approved_claim:
            return business
    return None


def _location_text(value):
    """Extract a readable location name from common JSON value shapes."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    if isinstance(value, dict):
        for key in ("en", "name", "label", "value"):
            if key in value:
                result = _location_text(value[key])
                if result:
                    return result
    return ""


def _state_name_for_dataset(value):
    """Map a dataset state label to the app's canonical state label."""
    candidate = normalize_location(_location_text(value))
    aliases = {
        "fct": "federal capital territory",
        "abuja": "federal capital territory",
        "abuja f c t": "federal capital territory",
        "abuja federal capital territory": "federal capital territory",
        "federal capital territory fct": "federal capital territory",
    }
    candidate = aliases.get(candidate, candidate)
    for state_name in NIGERIA_STATES:
        if normalize_location(state_name) == candidate:
            return state_name
    return ""


def load_nigeria_location_data():
    """Load and validate the state-to-LGA mapping from the local JSON file."""
    dataset_path = next(
        (path for path in LOCATION_DATA_CANDIDATES if os.path.isfile(path)),
        None,
    )
    print("--- LGA dataset path ->", dataset_path or "NOT FOUND")

    if not dataset_path:
        app.logger.error(
            "Nigeria LGA dataset not found. Expected it at: %s",
            LOCATION_DATA_CANDIDATES[0],
        )
        return {}

    try:
        with open(dataset_path, "r", encoding="utf-8-sig") as dataset_file:
            raw_data = json.load(dataset_file)
    except (OSError, json.JSONDecodeError) as exc:
        app.logger.exception("Could not read Nigeria LGA dataset: %s", exc)
        return {}

    grouped = {state_name: set() for state_name in NIGERIA_STATES}

    # Current clean dataset format: [{"state": "Abia", "lga": "Aba North"}, ...]
    if isinstance(raw_data, list):
        for item in raw_data:
            if not isinstance(item, dict):
                continue

            state_value = (
                item.get("state")
                or item.get("state_name")
                or item.get("stateName")
            )
            lga_value = (
                item.get("lga")
                or item.get("lga_name")
                or item.get("lgaName")
                or item.get("local_government")
                or item.get("localGovernment")
            )

            # Also support administrative datasets using name + parent.name.
            if lga_value is None:
                lga_value = item.get("name")
            if state_value is None:
                parent = item.get("parent")
                if isinstance(parent, dict):
                    state_value = parent.get("name") or parent.get("state")

            # Last-resort support for differently named state/LGA fields.
            if state_value is None or lga_value is None:
                for key, value in item.items():
                    key_lower = str(key).lower()
                    if state_value is None and "state" in key_lower:
                        state_value = value
                    if lga_value is None and ("lga" in key_lower or "local_government" in key_lower or "local government" in key_lower):
                        lga_value = value

            state_name = _state_name_for_dataset(state_value)
            lga_name = _location_text(lga_value)
            if state_name and lga_name:
                # Keep valid LGAs whose names happen to match their state name.
                grouped[state_name].add(lga_name)

    elif isinstance(raw_data, dict):
        # Support {"Oyo": ["Afijio", ...]} and {"Oyo": {"lgas": [...]}}.
        for raw_state, raw_values in raw_data.items():
            state_name = _state_name_for_dataset(raw_state)
            if not state_name:
                continue

            if isinstance(raw_values, dict):
                raw_values = raw_values.get("lgas", raw_values.get("local_governments", []))

            if not isinstance(raw_values, list):
                continue

            for raw_lga in raw_values:
                lga_name = _location_text(raw_lga)
                if lga_name:
                    grouped[state_name].add(lga_name)
    else:
        app.logger.error("Unsupported Nigeria LGA JSON root type: %s", type(raw_data).__name__)
        return {}

    cleaned = {
        state_name: sorted(grouped[state_name], key=str.casefold)
        for state_name in NIGERIA_STATES
        if grouped[state_name]
    }
    total_lgas = sum(len(values) for values in cleaned.values())
    print(
        "--- LGA dataset loaded ->",
        f"{len(cleaned)}/{len(NIGERIA_STATES)} states, {total_lgas} unique LGAs",
    )

    if len(cleaned) != len(NIGERIA_STATES):
        missing = [state for state in NIGERIA_STATES if not cleaned.get(state)]
        app.logger.warning("LGA dataset has no entries for these states: %s", missing)

    return cleaned


# Load after normalize_location and the canonical state list have been defined.
NIGERIA_LGAS = load_nigeria_location_data()


@app.route("/api/nigeria-locations")
def nigeria_locations_api():
    """Return the state list and local state-to-LGA mapping as JSON."""
    return jsonify({"states": NIGERIA_STATES, "lgas": NIGERIA_LGAS})


def make_state_slug(state_name):
    return re.sub(
        r"[^a-z0-9]+", "-", state_name.lower()
    ).strip("-")


def get_user_home_state(user=None):
    """Return a user's saved residence state, with a safe default for legacy accounts."""
    if user is None:
        user = get_user_by_id(session.get("user_id")) if session.get("user_id") is not None else None

    candidate = (user or {}).get("state_of_residence") or (user or {}).get("state") or "Oyo"
    normalized = normalize_location(candidate)

    # Common ways users may enter Abuja/FCT in their residence profile.
    if normalized in {"abj", "abuja", "fct", "federal capital territory", "federal capital"}:
        return "Federal Capital Territory"

    return next(
        (state_name for state_name in NIGERIA_STATES
         if normalize_location(state_name) == normalized),
        "Oyo",
    )


def get_state_cities(state):
    return STATE_CITIES.get(state, [])


def get_business_city(business):
    """
    Prefer an explicit city field when available.
    Otherwise read the existing location field, such as
    'BODIJA · IBADAN', and use its final component as the city.
    """
    explicit_city = business.get("city")
    if explicit_city:
        return normalize_location(explicit_city)

    location = str(business.get("location", "")).strip()
    if not location:
        return ""

    parts = re.split(r"\s*[·,]\s*", location)

    if len(parts) > 1:
        return normalize_location(parts[-1])

    normalized = normalize_location(location)

    # Support existing single-field locations that include
    # a neighbourhood followed by the city.
    known_cities = {
        normalize_location(city["name"])
        for city_list in STATE_CITIES.values()
        for city in city_list
    }

    for known_city in sorted(known_cities, key=len, reverse=True):
        if normalized == known_city or normalized.endswith(" " + known_city):
            return known_city

    return normalized


def get_business_state(business):
    """Prefer explicit state data; otherwise infer it from known cities."""
    explicit_state = business.get("state")
    if explicit_state:
        return normalize_location(explicit_state)

    city = get_business_city(business)

    for state_name, city_list in STATE_CITIES.items():
        for city_info in city_list:
            if normalize_location(city_info["name"]) == city:
                return normalize_location(state_name)

    return ""


def business_visible_in_state(business, state):
    """Premium listings can be discovered in purchased states; basic listings stay in their home state."""
    target = normalize_location(state)
    if not target:
        return False
    premium_states = business.get("premium_states") or []
    if business.get("premium_listing") and str(business.get("premium_status", "")).upper() == "ACTIVE":
        return any(normalize_location(item) == target for item in premium_states)
    return get_business_state(business) == target


def get_city_businesses(city, state=None):
    """Return public business listings belonging to the selected city."""
    requested_city = normalize_location(city)
    requested_state = normalize_location(state) if state else ""

    matching_businesses = []

    for business in get_business_discovery_data():
        business_city = get_business_city(business)
        business_state = get_business_state(business)

        if business_city != requested_city:
            continue

        if requested_state and business_state and business_state != requested_state:
            continue

        matching_businesses.append(business)

    return matching_businesses


@app.route("/explore/nigeria")
def explore_nigeria():
    return render_template(
        "explore_nigeria.html",
        states=NIGERIA_STATES
    )


@app.route("/state/<state_slug>")
def explore_state(state_slug):
    state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == state_slug.lower()),
        None
    )
    if state is None:
        return "State not found", 404

    state_cities = list(get_state_cities(state))
    state_businesses = [
        business for business in get_business_discovery_data()
        if business_visible_in_state(business, state)
    ]

    known_city_slugs = {item["slug"] for item in state_cities}
    for business in state_businesses:
        city_name = str(business.get("city") or "").strip()
        city_slug = re.sub(r"[^a-z0-9]+", "-", city_name.lower()).strip("-")
        if city_name and city_slug and city_slug not in known_city_slugs:
            state_cities.append({"name": city_name, "slug": city_slug})
            known_city_slugs.add(city_slug)

    return render_state_explore_page(
        state=state,
        businesses=state_businesses,
        cities=state_cities,
        selected_city=None
    )


@app.route("/state/<state_slug>/city/<city_slug>")
def explore_city(state_slug, city_slug):
    state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == state_slug.lower()),
        None
    )
    if state is None:
        return "State not found", 404

    city_info = next(
        (item for item in get_state_cities(state) if item["slug"] == city_slug.lower()),
        None
    )

    if city_info is None:
        city_info = next(
            (
                {
                    "name": str(business.get("city") or "").strip(),
                    "slug": re.sub(
                        r"[^a-z0-9]+", "-",
                        str(business.get("city") or "").strip().lower()
                    ).strip("-")
                }
                for business in get_business_discovery_data()
                if business_visible_in_state(business, state)
                and str(business.get("city") or "").strip()
                and re.sub(
                    r"[^a-z0-9]+", "-",
                    str(business.get("city") or "").strip().lower()
                ).strip("-") == city_slug.lower()
            ),
            None
        )

    if city_info is None:
        return "City not found", 404

    city = city_info["name"]
    city_businesses = get_city_businesses(city, state)

    return render_state_explore_page(
        state=state,
        businesses=city_businesses,
        cities=list(get_state_cities(state)),
        selected_city=city
    )


def render_state_explore_page(state, businesses, cities, selected_city=None):
    """Apply state/city-scoped search and filters for state discovery pages."""
    search_query = request.args.get("q", "").strip()
    selected_lga = request.args.get("lga", "").strip()
    selected_category = request.args.get("category", "all").strip().lower()

    all_businesses = list(businesses)
    categories = sorted({
        canonical_business_category(business.get("category", ""))
        for business in all_businesses
        if str(business.get("category", "")).strip()
    }, key=str.casefold)
    category_options = [
        {"name": category, "slug": category_slug(category)}
        for category in categories
    ]
    valid_category_slugs = {item["slug"] for item in category_options}
    if selected_category != "all" and selected_category not in valid_category_slugs:
        selected_category = "all"

    available_lgas = NIGERIA_LGAS.get(state, [])
    valid_lgas = {normalize_location(item): item for item in available_lgas}
    if selected_lga and normalize_location(selected_lga) not in valid_lgas:
        selected_lga = ""

    query_lower = search_query.casefold()
    filtered_businesses = []
    for business in all_businesses:
        if selected_category != "all" and not business_matches_category(business, selected_category):
            continue

        business_lga = str(business.get("lga", "")).strip()
        if selected_lga and normalize_location(business_lga) != normalize_location(selected_lga):
            continue

        if query_lower:
            searchable_text = " ".join([
                str(business.get("name", "")),
                str(business.get("category", "")),
                str(business.get("service_type", "")),
                str(business.get("location", "")),
                str(business.get("description", "")),
                str(business.get("about", "")),
                str(business.get("city", "")),
                str(business.get("lga", "")),
                str(business.get("state", ""))
            ]).casefold()
            if query_lower not in searchable_text:
                continue

        filtered_businesses.append(business)

    return render_template(
        "state_explore.html",
        state=state,
        businesses=filtered_businesses,
        cities=cities,
        selected_city=selected_city,
        search_query=search_query,
        selected_lga=selected_lga,
        selected_category=selected_category,
        lgas=available_lgas,
        categories=category_options,
        result_count=len(filtered_businesses),
        total_business_count=len(all_businesses)
    )



# =========================
# SERVICES DIRECTORY
# =========================

SERVICE_CATEGORIES = [
    {
        "title": "Home & Repairs", "icon": "home",
        "description": "Electricians, plumbers, carpenters, painters, AC technicians, generator repairs, CCTV installers and handymen.",
        "examples": ["Electricians", "Plumbers", "Carpenters", "Painters", "AC technicians", "Generator repairs", "CCTV installers", "Handymen"]
    },
    {
        "title": "Automotive Services", "icon": "car",
        "description": "Mechanics, auto electricians, car wash, vulcanizers, towing and vehicle inspection.",
        "examples": ["Mechanics", "Auto electricians", "Car wash", "Vulcanizers", "Towing", "Vehicle inspection"]
    },
    {
        "title": "Creative & Media", "icon": "camera",
        "description": "Photography, videography, graphic design, video editing, content creation and printing.",
        "examples": ["Photography", "Videography", "Graphic design", "Video editing", "Content creation", "Printing"]
    },
    {
        "title": "Professional & Digital", "icon": "laptop",
        "description": "Web development, software development, IT support, social media management, accounting, consulting and virtual assistance.",
        "examples": ["Web development", "Software development", "IT support", "Social media management", "Accounting", "Consulting", "Virtual assistance"]
    },
    {
        "title": "Everyday Services", "icon": "sparkles",
        "description": "Laundry, cleaning, hairdressers, barbers, makeup artists, tailors, catering and event planning.",
        "examples": ["Laundry", "Cleaning", "Hairdressers", "Barbers", "Makeup artists", "Tailors", "Catering", "Event planning"]
    },
    {
        "title": "Delivery & Logistics", "icon": "truck",
        "description": "Dispatch riders, moving services, delivery companies, freight, courier services and haulage.",
        "examples": ["Dispatch riders", "Moving services", "Delivery companies", "Freight", "Courier services", "Haulage"]
    },
    {
        "title": "Additional Personal Services", "icon": "heart",
        "description": "Massage & Spa, Personal Wellness, Private Appointments and eligible Adult-Oriented Services.",
        "examples": ["Massage & Spa", "Personal Wellness", "Private Appointments", "Adult-Oriented Services"]
    },
]

SERVICE_TYPES = [
    "Home & Repairs", "Automotive Services", "Creative & Media",
    "Professional & Digital", "Everyday Services", "Delivery & Logistics",
    "Massage & Spa", "Personal Wellness", "Private Appointments",
    "Adult-Oriented Services"
]

@app.route("/services")
def services_page():
    return render_template("services.html", service_categories=SERVICE_CATEGORIES)



# =========================
# MARKETPLACE
# =========================

MARKETPLACE_CATEGORIES = [
    {"name": "Fashion", "icon": "shirt"},
    {"name": "Electronics", "icon": "smartphone"},
    {"name": "Home & Living", "icon": "armchair"},
    {"name": "Pre-owned", "icon": "package"},
    {"name": "Handmade & Nigerian-made", "icon": "palette"},
    {"name": "Food & Groceries", "icon": "shopping-basket"},
    {"name": "Wholesale & Suppliers", "icon": "boxes"},
    {"name": "Solar & Power", "icon": "zap"},
    {"name": "Business & Industrial", "icon": "factory"},
    {"name": "Other", "icon": "grid-3x3"},
]

MARKETPLACE_UPLOAD_FOLDER = os.path.join("static", "uploads", "marketplace")
os.makedirs(MARKETPLACE_UPLOAD_FOLDER, exist_ok=True)


def get_marketplace_product(product_id):
    return next((item for item in marketplace_products if str(item.get("id")) == str(product_id)), None)


def public_marketplace_products():
    return [item for item in marketplace_products if item.get("status", "APPROVED") == "APPROVED"]


@app.route("/marketplace")
def marketplace_page():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "all").strip()
    section = request.args.get("section", "all").strip().lower()
    selected_state = request.args.get("state", "").strip()
    products = public_marketplace_products()
    if selected_state:
        products = [item for item in products if normalize_location(item.get("state")) == normalize_location(selected_state)]
    if category.lower() != "all":
        products = [item for item in products if normalize_location(item.get("category")) == normalize_location(category)]
    if section == "deals":
        products = [item for item in products if item.get("is_deal")]
    elif section == "wholesale":
        products = [item for item in products if item.get("wholesale")]
    elif section == "nearby":
        user = get_user_by_id(session.get("user_id")) if session.get("user_id") is not None else None
        user_state = normalize_location(get_user_home_state(user)) if user else ""
        if user_state:
            products = [item for item in products if normalize_location(item.get("state")) == user_state]
    if query:
        needle = query.casefold()
        products = [item for item in products if needle in " ".join(str(item.get(k, "")) for k in ("title", "description", "category", "brand", "seller_name", "state", "city", "lga")).casefold()]
    products.sort(key=lambda item: (bool(item.get("featured")), item.get("created_at", "")), reverse=True)
    _expire_spotlight_campaigns()
    ad_expiry = marketplace_premium_ad.get("expires_at") if marketplace_premium_ad else None
    ad_still_valid = True
    if ad_expiry:
        try:
            ad_still_valid = datetime.fromisoformat(ad_expiry.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except ValueError:
            ad_still_valid = False
    premium_ad = marketplace_premium_ad if marketplace_premium_ad.get("active") and ad_still_valid else None
    return render_template("marketplace.html", products=products, categories=MARKETPLACE_CATEGORIES,
                           search_query=query, selected_category=category, selected_section=section,
                           premium_ad=premium_ad, states=NIGERIA_STATES,
                           user=get_user_by_id(session.get("user_id")) if session.get("user_id") is not None else None)


@app.route("/admin/marketplace-ad", methods=["GET", "POST"])
def admin_marketplace_ad():
    """Admin-only editor for the paid premium Marketplace advertising panel."""
    if session.get("admin_logged_in") is not True:
        flash("Please sign in as an administrator to manage premium ads.", "error")
        return redirect(url_for("admin_login"))

    global marketplace_premium_ad
    if request.method == "POST":
        action = request.form.get("action", "save").strip().lower()
        if action == "disable":
            marketplace_premium_ad["active"] = False
            save_app_state()
            flash("Marketplace premium advertisement disabled.", "success")
            return redirect(url_for("admin_marketplace_ad"))

        headline = request.form.get("headline", "").strip()[:120]
        company = request.form.get("company", "").strip()[:100]
        description = request.form.get("description", "").strip()[:360]
        button_text = request.form.get("button_text", "Explore offer").strip()[:40] or "Explore offer"
        target_url = request.form.get("target_url", "").strip()[:500]
        image_url = request.form.get("image_url", "").strip()[:500]
        if not headline or not company or not description or not target_url:
            flash("Company, headline, description and destination URL are required.", "error")
        elif not target_url.startswith(("https://", "http://")):
            flash("The destination URL must begin with http:// or https://.", "error")
        elif image_url and not image_url.startswith(("https://", "http://", "/static/")):
            flash("Use an HTTPS image URL or a path beginning with /static/.", "error")
        else:
            marketplace_premium_ad = {
                "active": True,
                "company": company,
                "headline": headline,
                "description": description,
                "button_text": button_text,
                "target_url": target_url,
                "image_url": image_url,
                "updated_at": datetime.now().isoformat(timespec="seconds")
            }
            save_app_state()
            flash("Premium Marketplace ad published.", "success")
            return redirect(url_for("admin_marketplace_ad"))

    return render_template_string("""
    <!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Marketplace Premium Ad — THE ECOSYSTEM</title>
    <style>
    *{box-sizing:border-box}body{margin:0;background:#090909;color:#f7f4f2;font:15px Arial,sans-serif;padding:28px}
    main{max-width:760px;margin:auto;background:#111;padding:28px;border:1px solid #333;border-radius:18px}
    h1{margin-top:0}p{color:#aaa;line-height:1.6}.flash{padding:12px;border-radius:9px;background:#183323;color:#b9f4cf;margin:12px 0}
    label{display:block;font-size:13px;color:#ddd;margin:16px 0 7px}input,textarea{width:100%;padding:12px;border:1px solid #393939;background:#080808;color:white;border-radius:9px}
    textarea{min-height:90px;resize:vertical}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    button,a.button{display:inline-block;padding:12px 16px;border:0;border-radius:9px;background:#e8323f;color:white;font-weight:bold;text-decoration:none;cursor:pointer;margin-top:18px}
    button.disable{background:#252525;border:1px solid #555;margin-left:8px}
    small{color:#aaa}@media(max-width:600px){body{padding:12px}.grid{grid-template-columns:1fr}main{padding:18px}}
    </style></head><body><main><h1>Marketplace Premium Ad</h1>
    <p>Publish one large sponsored panel on the Marketplace homepage. Only an active campaign appears publicly. Use a company-owned destination URL and image URL.</p>
    {% with messages = get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash">{{ message }}</div>{% endfor %}{% endwith %}
    <form method="post">
      <div class="grid"><div><label>Company / Sponsor name</label><input name="company" maxlength="100" required value="{{ ad.get('company','') }}"></div>
      <div><label>Headline</label><input name="headline" maxlength="120" required value="{{ ad.get('headline','') }}"></div></div>
      <label>Short description</label><textarea name="description" maxlength="360" required>{{ ad.get('description','') }}</textarea>
      <div class="grid"><div><label>Button text</label><input name="button_text" maxlength="40" value="{{ ad.get('button_text','Explore offer') }}"></div>
      <div><label>Destination URL</label><input type="url" name="target_url" required placeholder="https://company.example/offer" value="{{ ad.get('target_url','') }}"></div></div>
      <label>Optional banner image URL or /static/ path</label><input name="image_url" placeholder="https://... or /static/uploads/..." value="{{ ad.get('image_url','') }}">
      <button type="submit" name="action" value="save">Publish premium ad</button>
      <button class="disable" type="submit" name="action" value="disable">Disable ad</button>
    </form>
    <p><small>Current status: {{ 'ACTIVE' if ad.get('active') else 'NOT ACTIVE' }}. Admin access is required.</small></p>
    <a class="button" href="{{ url_for('marketplace_page') }}">View Marketplace</a>
    </main></body></html>
    """, ad=marketplace_premium_ad)



# =========================
# PAID BUSINESS SPOTLIGHT (PAYSTACK)
# =========================

SPOTLIGHT_PLANS = {
    "starter": {"name": "Spotlight Starter", "amount": 1500000, "duration_days": 7, "benefits": ["Premium Marketplace panel", "Business banner and call-to-action", "7 days of visibility"]},
    "growth": {"name": "Spotlight Growth", "amount": 3500000, "duration_days": 14, "benefits": ["Premium Marketplace panel", "Priority placement", "14 days of visibility"]},
    "premium": {"name": "Spotlight Premium", "amount": 6500000, "duration_days": 30, "benefits": ["Premium Marketplace panel", "Top-priority placement", "30 days of visibility"]},
}


def _spotlight_user():
    uid = session.get("user_id")
    return get_user_by_id(uid) if uid is not None else None


def _paystack_request(path, payload=None, method="GET"):
    secret = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("Paystack is not configured. Add PAYSTACK_SECRET_KEY to your .env file.")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        "https://api.paystack.co" + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/155.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        app.logger.warning("Paystack API returned %s: %s", exc.code, detail[:500])
        raise RuntimeError("Paystack could not complete the request. Check your keys and try again.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Could not reach Paystack. Please try again.") from exc


@app.route("/spotlight/plans")
def spotlight_plans():
    """Show the standalone plans page when installed, with an embedded fallback."""
    plans_template_path = os.path.join(app.root_path, app.template_folder or "templates", "spotlight_plans.html")
    if os.path.isfile(plans_template_path):
        return render_template("spotlight_plans.html", plans=SPOTLIGHT_PLANS, user=_spotlight_user())
    # Keeps the plans page available even if templates/spotlight_plans.html has not been copied yet.
    return render_template_string(SPOTLIGHT_PLANS_TEMPLATE, plans=SPOTLIGHT_PLANS, user=_spotlight_user())


@app.route("/spotlight/pay/<plan_id>", methods=["POST"])
def spotlight_pay(plan_id):
    user = _spotlight_user()
    if not user:
        flash("Please sign in before purchasing a spotlight plan.", "error")
        return redirect(url_for("login", next=url_for("spotlight_plans")))
    plan = SPOTLIGHT_PLANS.get(plan_id)
    if not plan:
        flash("That spotlight plan is not available.", "error")
        return redirect(url_for("spotlight_plans"))
    email = (user.get("email") or "").strip()
    if not email:
        flash("Your account needs an email address before checkout.", "error")
        return redirect(url_for("spotlight_plans"))
    reference = "ECO-SPOT-" + secrets.token_hex(12).upper()
    payment = {"reference": reference, "user_id": user.get("id"), "plan_id": plan_id,
               "amount_kobo": plan["amount"], "status": "INITIALIZING", "created_at": datetime.now(timezone.utc).isoformat()}
    spotlight_payments.append(payment)
    try:
        result = _paystack_request("/transaction/initialize", {
            "email": email, "amount": plan["amount"], "reference": reference,
            "callback_url": url_for("spotlight_payment_callback", _external=True),
            "metadata": {"purpose": "business_spotlight", "user_id": str(user.get("id")), "plan_id": plan_id}
        }, "POST")
        if not result.get("status") or not result.get("data", {}).get("authorization_url"):
            payment["status"] = "INITIALIZATION_FAILED"
            save_app_state()
            flash("Paystack could not start checkout. Please try again.", "error")
            return redirect(url_for("spotlight_plans"))
        payment["status"] = "PENDING"
        payment["access_code"] = result["data"].get("access_code", "")
        save_app_state()
        return redirect(result["data"]["authorization_url"])
    except RuntimeError as exc:
        payment["status"] = "INITIALIZATION_FAILED"
        save_app_state()
        flash(str(exc), "error")
        return redirect(url_for("spotlight_plans"))


@app.route("/spotlight/payment/callback")
def spotlight_payment_callback():
    reference = request.args.get("reference", "").strip()
    if not reference:
        flash("Payment reference is missing. Please check your payment status.", "error")
        return redirect(url_for("spotlight_plans"))
    payment = next((p for p in spotlight_payments if p.get("reference") == reference), None)
    if not payment:
        flash("We could not find this payment reference.", "error")
        return redirect(url_for("spotlight_plans"))
    try:
        result = _paystack_request("/transaction/verify/" + urllib.parse.quote(reference, safe=""))
        data = result.get("data") or {}
        expected = int(payment.get("amount_kobo", -1))
        if (result.get("status") and data.get("status") == "success"
                and data.get("reference") == reference
                and int(data.get("amount", -2)) == expected
                and str((data.get("metadata") or {}).get("user_id", "")) == str(payment.get("user_id"))):
            payment["status"] = "SUCCESS"
            payment["verified_at"] = datetime.now(timezone.utc).isoformat()
            payment["paystack_transaction_id"] = str(data.get("id", ""))
            payment["channel"] = data.get("channel", "")
            campaign = next((c for c in spotlight_campaigns if c.get("payment_reference") == reference), None)
            if not campaign:
                campaign = {"id": uuid.uuid4().hex, "user_id": payment["user_id"], "plan_id": payment["plan_id"],
                            "payment_reference": reference, "payment_status": "SUCCESS", "status": "AWAITING_SUBMISSION",
                            "created_at": datetime.now(timezone.utc).isoformat()}
                spotlight_campaigns.append(campaign)
            save_app_state()
            session["spotlight_campaign_id"] = campaign["id"]
            flash("Payment verified successfully. Now submit your business spotlight details.", "success")
            return redirect(url_for("spotlight_submit", campaign_id=campaign["id"]))
        payment["status"] = "FAILED_OR_UNVERIFIED"
        save_app_state()
        flash("Payment has not been verified as successful. No submission access has been unlocked.", "error")
    except (RuntimeError, ValueError, TypeError) as exc:
        app.logger.warning("Spotlight payment verification issue: %s", exc)
        flash("We could not verify payment right now. Please retry using the same reference or contact support.", "error")
    return redirect(url_for("spotlight_plans"))


@app.route("/spotlight/submit/<campaign_id>", methods=["GET", "POST"])
def spotlight_submit(campaign_id):
    user = _spotlight_user()
    if not user:
        flash("Please sign in to submit your paid campaign.", "error")
        return redirect(url_for("login", next=request.path))
    campaign = next((c for c in spotlight_campaigns if c.get("id") == campaign_id and str(c.get("user_id")) == str(user.get("id"))), None)
    if not campaign or campaign.get("payment_status") != "SUCCESS":
        flash("A verified spotlight payment is required before submitting campaign details.", "error")
        return redirect(url_for("spotlight_plans"))
    if request.method == "POST":
        company = request.form.get("company", "").strip()[:120]
        headline = request.form.get("headline", "").strip()[:150]
        description = request.form.get("description", "").strip()[:700]
        target_url = request.form.get("target_url", "").strip()[:500]
        button_text = request.form.get("button_text", "Explore offer").strip()[:40] or "Explore offer"
        image_url = request.form.get("image_url", "").strip()[:500]
        if not company or not headline or not description:
            flash("Company name, headline, and description are required.", "error")
        elif target_url and not target_url.startswith(("https://", "http://")):
            flash("The destination must begin with https:// or http://.", "error")
        elif image_url and not image_url.startswith(("https://", "http://", "/static/")):
            flash("Use an HTTPS image URL or a path beginning with /static/.", "error")
        else:
            campaign.update({"company": company, "headline": headline, "description": description,
                             "target_url": target_url or url_for("discover", _external=True),
                             "button_text": button_text, "image_url": image_url,
                             "status": "PENDING_REVIEW", "submitted_at": datetime.now(timezone.utc).isoformat()})
            save_app_state()
            flash("Your campaign has been submitted for admin review. It will appear after approval.", "success")
            return redirect(url_for("spotlight_status", campaign_id=campaign_id))
    submit_template_path = os.path.join(app.root_path, app.template_folder or "templates", "spotlight_submit.html")
    if os.path.isfile(submit_template_path):
        return render_template("spotlight_submit.html", campaign=campaign, plan=SPOTLIGHT_PLANS[campaign["plan_id"]])
    return render_template_string(SPOTLIGHT_SUBMIT_TEMPLATE, campaign=campaign, plan=SPOTLIGHT_PLANS[campaign["plan_id"]])


@app.route("/spotlight/status/<campaign_id>")
def spotlight_status(campaign_id):
    user = _spotlight_user()
    campaign = next(
        (c for c in spotlight_campaigns
         if str(c.get("id")) == str(campaign_id)
         and user and str(c.get("user_id")) == str(user.get("id"))),
        None,
    )
    if not campaign:
        flash("Campaign not found. Please sign in with the account used to purchase Spotlight.", "error")
        return redirect(url_for("spotlight_plans"))

    plan = SPOTLIGHT_PLANS.get(campaign.get("plan_id"), {})
    status_template_path = os.path.join(
        app.root_path, app.template_folder or "templates", "spotlight_status.html"
    )
    if os.path.isfile(status_template_path):
        return render_template("spotlight_status.html", campaign=campaign, plan=plan)
    # Embedded fallback keeps the status page working if the template was not copied.
    return render_template_string(SPOTLIGHT_STATUS_TEMPLATE, campaign=campaign, plan=plan)


@app.route("/admin/spotlight", methods=["GET", "POST"])
def admin_spotlight_review():
    """Review paid Spotlight campaigns and always return to a rendered queue."""
    global marketplace_premium_ad

    if not admin_required():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        campaign_id = str(request.form.get("campaign_id", "")).strip()
        action = str(request.form.get("action", "")).strip().lower()
        campaign = next(
            (c for c in spotlight_campaigns if str(c.get("id", "")) == campaign_id),
            None,
        )

        if campaign is None:
            flash("Spotlight campaign could not be found. Refresh the queue and try again.", "error")
        elif action == "approve":
            if campaign.get("payment_status") != "SUCCESS":
                flash("Approval blocked: Paystack payment is not verified as successful.", "error")
            elif campaign.get("status") != "PENDING_REVIEW":
                flash("This campaign is no longer pending review.", "error")
            else:
                plan = SPOTLIGHT_PLANS.get(campaign.get("plan_id"), SPOTLIGHT_PLANS["starter"])
                now = datetime.now(timezone.utc)
                expires_at = (now + timedelta(days=plan["duration_days"])).isoformat()
                campaign.update({
                    "status": "APPROVED",
                    "approved_at": now.isoformat(),
                    "reviewed_at": now.isoformat(),
                    "starts_at": now.isoformat(),
                    "expires_at": expires_at,
                })
                marketplace_premium_ad = {
                    "active": True,
                    "campaign_id": campaign.get("id"),
                    "company": campaign.get("company", ""),
                    "headline": campaign.get("headline", ""),
                    "description": campaign.get("description", ""),
                    "button_text": campaign.get("button_text", "Explore offer"),
                    "target_url": campaign.get("target_url", ""),
                    "image_url": campaign.get("image_url", ""),
                    "expires_at": expires_at,
                    "plan_id": campaign.get("plan_id"),
                }
                flash("Spotlight campaign approved and published to Marketplace.", "success")
        elif action == "reject":
            if campaign.get("status") != "PENDING_REVIEW":
                flash("Only campaigns pending review can be rejected.", "error")
            else:
                campaign.update({
                    "status": "REJECTED",
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                })
                flash("Spotlight campaign rejected. Payment remains recorded for support/refund handling.", "success")
        else:
            flash("Unsupported Spotlight action. Choose approve or reject.", "error")

        # Persistence failures should not produce a blank browser response.
        try:
            save_app_state()
        except Exception:
            app.logger.exception("Could not persist Spotlight moderation action")
            flash("The action was processed in memory, but saving it failed. Check the Flask terminal/database permissions.", "error")

        return redirect(url_for("admin_spotlight_review"), code=303)

    _expire_spotlight_campaigns()
    return render_template_string(
        SPOTLIGHT_ADMIN_TEMPLATE,
        campaigns=spotlight_campaigns,
        plans=SPOTLIGHT_PLANS,
    )


def _expire_spotlight_campaigns():
    global marketplace_premium_ad
    now = datetime.now(timezone.utc)
    for campaign in spotlight_campaigns:
        if campaign.get("status") == "APPROVED" and campaign.get("expires_at"):
            try:
                if datetime.fromisoformat(campaign["expires_at"].replace("Z", "+00:00")) <= now:
                    campaign["status"] = "EXPIRED"
                    if marketplace_premium_ad.get("campaign_id") == campaign.get("id"):
                        marketplace_premium_ad = {}
            except ValueError:
                continue


SPOTLIGHT_BASE_STYLE = """<style>*{box-sizing:border-box}body{margin:0;background:#090909;color:#f7f4f2;font:15px Arial,sans-serif;padding:24px}main{max-width:980px;margin:auto}.panel{background:#111;border:1px solid #303030;border-radius:18px;padding:24px;margin:15px 0}.muted,p{color:#b8b0b0;line-height:1.6}h1{font-size:clamp(28px,5vw,42px);margin:8px 0}.brand{color:white;text-decoration:none;font-weight:900}.brand span{color:#ef4444}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.plan{background:#121212;border:1px solid #383838;border-radius:16px;padding:20px}.plan.featured{border-color:#ef4444;box-shadow:0 0 0 1px #ef444422}.price{font-size:30px;font-weight:900;color:#fff;margin:12px 0}.btn,button{display:inline-block;border:0;border-radius:9px;background:#ef3434;color:white;padding:12px 16px;font-weight:800;text-decoration:none;cursor:pointer}.btn.secondary,button.secondary{background:#252525;border:1px solid #444}.field{margin:14px 0}label{display:block;margin-bottom:7px;color:#ddd;font-size:13px}input,textarea{width:100%;padding:12px;border-radius:9px;border:1px solid #393939;background:#080808;color:white;font:inherit}textarea{min-height:110px}small{color:#aaa}.flash{padding:12px;background:#241214;border-radius:9px;margin:10px 0}@media(max-width:700px){body{padding:12px}.grid{grid-template-columns:1fr}.panel{padding:17px}}"""

SPOTLIGHT_PLANS_TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Spotlight Plans — THE ECOSYSTEM</title>""" + SPOTLIGHT_BASE_STYLE + """</head><body><main><a class='brand' href='{{ url_for("marketplace_page") }}'>THE <span>ECOSYSTEM</span></a><section class='panel'><small>PREMIUM BUSINESS PROMOTION</small><h1>Put your brand in the spotlight.</h1><p>Choose how long your business gets featured in the Marketplace premium advertising panel. Payment is verified by Paystack before campaign submission unlocks.</p>{% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class='flash'>{{ message }}</div>{% endfor %}{% endwith %}<div class='grid'>{% for plan_id,plan in plans.items() %}<article class='plan {% if plan_id=="growth" %}featured{% endif %}'><small>{{ plan.name }}</small><div class='price'>₦{{ '{:,.0f}'.format(plan.amount/100) }}</div><p>{{ plan.duration_days }} days of spotlight</p><ul>{% for benefit in plan.benefits %}<li>{{ benefit }}</li>{% endfor %}</ul><form method='post' action='{{ url_for("spotlight_pay", plan_id=plan_id) }}'><button type='submit'>Choose plan →</button></form></article>{% endfor %}</div></section></main></body></html>"""

SPOTLIGHT_SUBMIT_TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Submit Spotlight — THE ECOSYSTEM</title>""" + SPOTLIGHT_BASE_STYLE + """</head><body><main><a class='brand' href='{{ url_for("marketplace_page") }}'>THE <span>ECOSYSTEM</span></a><section class='panel'><small>PAYMENT VERIFIED · {{ plan.name }}</small><h1>Tell Nigeria about your brand.</h1><p>Your payment is verified. Submit the campaign details below for admin approval.</p>{% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class='flash'>{{ message }}</div>{% endfor %}{% endwith %}<form method='post'><div class='field'><label>Company / business name *</label><input name='company' maxlength='120' required value='{{ campaign.get("company", "") }}'></div><div class='field'><label>Headline *</label><input name='headline' maxlength='150' required value='{{ campaign.get("headline", "") }}'></div><div class='field'><label>Short description *</label><textarea name='description' maxlength='700' required>{{ campaign.get("description", "") }}</textarea></div><div class='field'><label>Destination website or page (optional)</label><input type='url' name='target_url' placeholder='https://...' value='{{ campaign.get("target_url", "") }}'></div><div class='field'><label>Button text</label><input name='button_text' maxlength='40' value='{{ campaign.get("button_text", "Explore offer") }}'></div><div class='field'><label>Banner image URL or /static/ image path (optional)</label><input name='image_url' value='{{ campaign.get("image_url", "") }}' placeholder='https://... or /static/uploads/...'></div><button type='submit'>Submit for review →</button></form></section></main></body></html>"""

SPOTLIGHT_STATUS_TEMPLATE = """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Spotlight Submission Received — THE ECOSYSTEM</title>""" + SPOTLIGHT_BASE_STYLE + """</head><body><main><a class='brand' href='{{ url_for("marketplace_page") }}'>THE <span>ECOSYSTEM</span></a><section class='panel' style='max-width:760px;margin:28px auto;padding:clamp(22px,5vw,42px)'><div style='width:58px;height:58px;border-radius:50%;display:grid;place-items:center;background:#123321;color:#72e6a1;font-size:30px;font-weight:900'>✓</div><small style='display:block;margin-top:20px'>{{ plan.get('name','Spotlight campaign') }}</small><h1>Submission received!</h1><p>Your business Spotlight details have been submitted successfully. Our admin team will review your campaign before it appears on the Marketplace.</p><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:22px 0'><div style='background:#090909;border:1px solid #333;border-radius:12px;padding:16px'><small>CAMPAIGN STATUS</small><h2 style='font-size:18px;margin:8px 0;color:#ffd166'>{{ campaign.get('status','PENDING_REVIEW')|replace('_',' ') }}</h2></div><div style='background:#090909;border:1px solid #333;border-radius:12px;padding:16px'><small>PAYMENT STATUS</small><h2 style='font-size:18px;margin:8px 0;color:#72e6a1'>{{ campaign.get('payment_status','PENDING')|replace('_',' ') }}</h2></div></div><div style='border-top:1px solid #303030;padding-top:18px'><p><strong>Business:</strong> {{ campaign.get('company','Your business') }}</p><p><strong>Campaign headline:</strong> {{ campaign.get('headline','—') }}</p>{% if campaign.get('submitted_at') %}<p><strong>Submitted:</strong> {{ campaign.get('submitted_at')[:19].replace('T',' ') }} UTC</p>{% endif %}{% if campaign.get('expires_at') %}<p><strong>Expires:</strong> {{ campaign.get('expires_at') }}</p>{% endif %}</div><p style='background:#1b1710;border:1px solid #59451d;border-radius:10px;padding:14px;color:#f5d99a'>Your campaign is not live yet. It will appear publicly after admin approval.</p><a class='btn' href='{{ url_for("marketplace_page") }}'>Back to Marketplace</a> <a class='btn secondary' href='{{ url_for("spotlight_plans") }}'>View Spotlight plans</a></section></main></body></html>"""

SPOTLIGHT_ADMIN_TEMPLATE = """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Spotlight Review — THE ECOSYSTEM</title>""" + SPOTLIGHT_BASE_STYLE + """<style>.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:16px 0}.meta div{background:#090909;border:1px solid #303030;border-radius:10px;padding:12px;overflow-wrap:anywhere}.meta small{display:block;color:#aaa;margin-bottom:5px}.status{display:inline-block;border:1px solid #555;border-radius:99px;padding:4px 9px;font-size:12px}.success{border-color:#276b49;color:#72e6a1}.pending{border-color:#80632c;color:#ffd166}.rejected{border-color:#823333;color:#ff9292}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.actions form{display:inline-flex}.flash.error{border:1px solid #823333;color:#ffaaaa}.flash.success{border:1px solid #276b49;color:#a7f0c3}.url{overflow-wrap:anywhere}</style></head><body><main><a class='brand' href='{{ url_for("admin_dashboard") }}'>THE <span>ECOSYSTEM</span> · ADMIN</a><section class='panel'><small>ADVERTISING OPERATIONS</small><h1>Paid Spotlight review</h1><p>Review the complete campaign, payment record, submitter and creative before publishing. A campaign can only be approved after verified successful payment.</p><p><a class='btn secondary' href='{{ url_for("admin_moderator") }}'>Back to Moderator</a> <a class='btn secondary' href='{{ url_for("admin_dashboard") }}'>Dashboard</a></p>{% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class='flash {{ category }}'>{{ message }}</div>{% endfor %}{% endwith %}{% for c in campaigns|reverse %}<article class='plan' style='margin:14px 0'><div style='display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap'><small>{{ plans.get(c.get('plan_id',''),{}).get('name',c.get('plan_id','Spotlight plan')) }}</small><span class='status {{ "success" if c.get("status")=="APPROVED" else "rejected" if c.get("status")=="REJECTED" else "pending" }}'>{{ c.get('status','UNKNOWN')|replace('_',' ') }}</span></div><h2>{{ c.get('company') or 'Payment awaiting campaign details' }}</h2><p><strong>{{ c.get('headline') or 'No headline submitted yet' }}</strong></p><p>{{ c.get('description') or 'No campaign description submitted yet.' }}</p><div class='meta'><div><small>Plan price</small><strong>₦{{ '{:,.0f}'.format(plans.get(c.get('plan_id',''),{}).get('amount',0)/100) }}</strong></div><div><small>Duration</small><strong>{{ plans.get(c.get('plan_id',''),{}).get('duration_days','—') }} days</strong></div><div><small>Payment status</small><strong>{{ c.get('payment_status','UNKNOWN') }}</strong></div><div><small>Payment reference</small><span>{{ c.get('payment_reference') or c.get('reference') or '—' }}</span></div><div><small>Campaign ID</small><span>{{ c.get('id','—') }}</span></div><div><small>Submitter user ID</small><span>{{ c.get('user_id','—') }}</span></div><div><small>Created</small><span>{{ c.get('created_at') or '—' }}</span></div><div><small>Submitted for review</small><span>{{ c.get('submitted_at') or '—' }}</span></div><div><small>Approved / reviewed</small><span>{{ c.get('reviewed_at') or c.get('approved_at') or '—' }}</span></div><div><small>Starts / expires</small><span>{{ c.get('starts_at') or '—' }} / {{ c.get('expires_at') or '—' }}</span></div></div>{% if c.get('target_url') %}<p class='url'><strong>Destination:</strong> <a href='{{ c.get("target_url") }}' target='_blank' rel='noopener noreferrer'>{{ c.get('target_url') }}</a></p>{% endif %}{% if c.get('image_url') %}<p class='url'><strong>Creative image:</strong> <a href='{{ c.get("image_url") }}' target='_blank' rel='noopener noreferrer'>Open image</a></p>{% if c.get('image_url').startswith('/static/') %}<img src='{{ c.get("image_url") }}' alt='Spotlight creative preview' style='display:block;max-width:100%;max-height:240px;border-radius:10px;margin-top:10px'>{% endif %}{% endif %}{% if c.get('status')=='PENDING_REVIEW' %}<div class='actions'><form method='post' action='{{ url_for("admin_spotlight_review") }}' onsubmit='return confirm("Approve and publish this paid Spotlight campaign?")'><input type='hidden' name='campaign_id' value='{{ c.get("id","") }}'><button type='submit' name='action' value='approve' {% if c.get('payment_status')!='SUCCESS' %}disabled title='Payment must be verified first' style='opacity:.45;cursor:not-allowed'{% endif %}>Approve &amp; publish</button></form><form method='post' action='{{ url_for("admin_spotlight_review") }}' onsubmit='return confirm("Reject this Spotlight campaign?")'><input type='hidden' name='campaign_id' value='{{ c.get("id","") }}'><button class='secondary' type='submit' name='action' value='reject'>Reject</button></form></div>{% endif %}</article>{% else %}<p class='empty'>No Spotlight campaigns have been submitted yet.</p>{% endfor %}</section></main></body></html>"""

@app.route("/marketplace/sell", methods=["GET", "POST"])
def marketplace_sell():
    user_id = session.get("user_id")
    if user_id is None or get_user_by_id(user_id) is None:
        flash("Please log in before listing a product.", "error")
        return redirect(url_for("login", next=url_for("marketplace_sell")))
    user = get_user_by_id(user_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        price_raw = request.form.get("price", "").strip().replace(",", "")
        condition = request.form.get("condition", "New").strip()
        state_raw = request.form.get("state", "").strip()
        city = request.form.get("city", "").strip()
        lga = request.form.get("lga", "").strip()
        description = request.form.get("description", "").strip()
        contact = request.form.get("contact", "").strip()
        brand = request.form.get("brand", "").strip()
        wholesale = request.form.get("wholesale") == "on"
        is_deal = request.form.get("is_deal") == "on"
        image_file = request.files.get("image")
        valid_state = next((item for item in NIGERIA_STATES if normalize_location(item) == normalize_location(state_raw)), None)
        try:
            price = float(price_raw)
            valid_price = price >= 0
        except ValueError:
            price, valid_price = 0, False
        valid_categories = {item["name"] for item in MARKETPLACE_CATEGORIES}
        if not all([title, category in valid_categories, valid_state, city, lga, description, contact]) or not valid_price:
            flash("Complete all required fields and enter a valid price.", "error")
            return render_template("marketplace_sell.html", categories=MARKETPLACE_CATEGORIES, states=NIGERIA_STATES, user=user)
        image_path = ""
        if image_file and image_file.filename:
            if not allowed_file(image_file.filename):
                flash("Product image must be JPG, JPEG, PNG or WEBP.", "error")
                return render_template("marketplace_sell.html", categories=MARKETPLACE_CATEGORIES, states=NIGERIA_STATES, user=user)
            image_file.stream.seek(0, os.SEEK_END)
            image_size = image_file.stream.tell()
            image_file.stream.seek(0)
            if image_size > MAX_PHOTO_SIZE:
                flash("Product image must be 5 MB or smaller.", "error")
                return render_template("marketplace_sell.html", categories=MARKETPLACE_CATEGORIES, states=NIGERIA_STATES, user=user)
            filename = f"product_{uuid.uuid4().hex}_{secure_filename(image_file.filename)}"
            image_file.save(os.path.join(MARKETPLACE_UPLOAD_FOLDER, filename))
            image_path = f"uploads/marketplace/{filename}"
        product_id = max([int(item.get("id", 0)) for item in marketplace_products] + [0]) + 1
        product = {
            "id": product_id, "title": title, "category": category, "price": price,
            "condition": condition if condition in {"New", "Used", "Refurbished", "Handmade"} else "New",
            "state": valid_state, "city": city, "lga": lga, "description": description,
            "contact": contact, "brand": brand, "wholesale": wholesale, "is_deal": is_deal,
            "image_path": image_path, "user_id": user_id, "seller_name": user.get("name", "Seller"),
            "status": "PENDING", "created_at": datetime.now().isoformat(timespec="seconds"), "featured": False,
        }
        marketplace_products.append(product)
        notify_admin("Marketplace product awaiting review", f"{product.get('title')} was submitted by {user.get('name', 'a seller')} and is awaiting moderation.", "marketplace_product", product_id)
        save_app_state()
        flash("Your product has been submitted for review. It will appear after approval.", "success")
        return redirect(url_for("marketplace_product", product_id=product_id))
    return render_template("marketplace_sell.html", categories=MARKETPLACE_CATEGORIES, states=NIGERIA_STATES, user=user)


@app.route("/marketplace/product/<int:product_id>")
def marketplace_product(product_id):
    product = get_marketplace_product(product_id)
    if product is None or product.get("status", "APPROVED") != "APPROVED":
        return "Product not found", 404
    seller_products = [item for item in public_marketplace_products() if item.get("user_id") == product.get("user_id") and item.get("id") != product_id][:4]
    return render_template("marketplace_product.html", product=product, seller_products=seller_products)


@app.route("/marketplace/product/<int:product_id>/report", methods=["POST"])
def marketplace_report_product(product_id):
    product = get_marketplace_product(product_id)
    if product is None:
        return "Product not found", 404
    reason = request.form.get("reason", "Suspicious listing").strip()[:300]
    reporter = get_user_by_id(session.get("user_id")) if session.get("user_id") is not None else None
    reports.append({"id": max([int(item.get("id", 0)) for item in reports] + [0]) + 1,
                    "target_type": "marketplace_product", "target_id": product_id,
                    "reason": reason, "reporter_id": session.get("user_id"),
                    "reporter_name": reporter.get("name", "Community member") if reporter else "Community member",
                    "created_at": datetime.now().strftime("%b %d, %Y"), "status": "OPEN"})
    notify_admin("Marketplace product report received", f"A report was submitted for marketplace listing {product.get('title')}: {reason}", "report", reports[-1].get("id"))
    save_app_state()
    flash("Thanks. Your report has been sent for review.", "success")
    return redirect(url_for("marketplace_product", product_id=product_id))


# =========================
# BUSINESSES / DISCOVERY
# =========================


def _business_search_intent(query):
    """Interpret common category and location phrases instead of matching the whole query literally."""
    raw = str(query or "").strip()
    working = normalize_location(raw)
    category_groups = {
        "food": {
            "food", "foods", "restaurant", "restaurants", "resto", "dining",
            "eatery", "eateries", "cafe", "cafes", "coffee shop", "coffee shops",
            "grill", "grills", "food spot", "food spots", "takeaway", "takeaways",
        },
        "hotel": {
            "hotel", "hotels", "lodging", "accommodation", "accommodations",
            "guest house", "guest houses", "resort", "resorts",
        },
        "fashion": {
            "fashion", "clothing", "clothes", "beauty", "barber", "barbers",
            "barbershop", "barbershops", "salon", "salons", "stylist", "stylists",
        },
        "services": {"service", "services", "repair", "repairs", "mechanic", "mechanics"},
        "shopping": {"shopping", "shop", "shops", "store", "stores", "retail", "market", "markets"},
        "tech": {"tech", "technology", "software", "computer", "computers", "electronics"},
        "entertainment": {"entertainment", "fun", "nightlife", "games", "gaming", "music"},
        "health": {"health", "healthcare", "hospital", "hospitals", "clinic", "clinics", "pharmacy", "pharmacies"},
        "education": {"education", "school", "schools", "training", "tutor", "tutors"},
        "transport": {"transport", "transportation", "logistics", "delivery", "deliveries"},
    }
    category_alias_to_group = {
        alias: group for group, aliases in category_groups.items() for alias in aliases
    }
    found_groups = {
        group for alias, group in category_alias_to_group.items()
        if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", working)
    }
    # Prefer explicit categories represented by the query; multiple category words may coexist.
    found_location = None
    found_state = None
    location_phrases = []

    # Detect the longest known city names first, so multi-word cities are not partially matched.
    known_cities = []
    for state_name, city_list in STATE_CITIES.items():
        for city_info in city_list:
            city_name = str(city_info.get("name", "")).strip()
            if city_name:
                known_cities.append((city_name, state_name))
    for city_name, state_name in sorted(known_cities, key=lambda item: len(item[0]), reverse=True):
        normalized_city = normalize_location(city_name)
        if normalized_city and re.search(r"(?<![a-z0-9])" + re.escape(normalized_city) + r"(?![a-z0-9])", working):
            found_location = normalized_city
            found_state = normalize_location(state_name)
            location_phrases.append(city_name)
            break

    if not found_location:
        for state_name in sorted(NIGERIA_STATES, key=len, reverse=True):
            normalized_state = normalize_location(state_name)
            aliases = {normalized_state}
            if normalized_state == "federal capital territory":
                aliases.update({"abuja", "fct", "federal capital"})
            for alias in sorted(aliases, key=len, reverse=True):
                if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", working):
                    found_state = normalized_state
                    location_phrases.append(alias)
                    break
            if found_state:
                break

    # Remove category, location and connecting words before matching remaining keywords.
    remainder = working
    # Remove any state named in the query as well, including queries like
    # "restaurants in Ibadan, Oyo" where the city was detected first.
    for state_name in NIGERIA_STATES:
        normalized_state = normalize_location(state_name)
        state_aliases = {normalized_state}
        if normalized_state == "federal capital territory":
            state_aliases.update({"abuja", "fct", "federal capital"})
        location_phrases.extend(state_aliases)
    for phrase in sorted(set(location_phrases), key=len, reverse=True):
        remainder = re.sub(r"(?<![a-z0-9])" + re.escape(normalize_location(phrase)) + r"(?![a-z0-9])", " ", remainder)
    for alias in sorted(category_alias_to_group, key=len, reverse=True):
        remainder = re.sub(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", " ", remainder)
    remainder = re.sub(r"\b(in|at|around|near|within|the|a|an|for|of|and|to|please|find|show|me|business|businesses|place|places)\b", " ", remainder)
    keywords = [word for word in re.findall(r"[a-z0-9]+", remainder) if len(word) > 1]

    return {
        "groups": found_groups,
        "city": found_location,
        "state": found_state,
        "keywords": keywords,
    }


def _business_matches_search_category(business, groups):
    if not groups:
        return True
    category_text = normalize_location(" ".join([
        str(business.get("category", "")),
        str(business.get("service_type", "")),
        str(business.get("name", "")),
        str(business.get("description", "")),
    ]))
    category = normalize_location(business.get("category", ""))
    group_aliases = {
        "food": {"food", "restaurant", "restaurants", "dining", "eatery", "cafe", "grill"},
        "hotel": {"hotel", "hotels", "lodging", "accommodation", "guest house", "resort"},
        "fashion": {"fashion", "clothing", "beauty", "barber", "barbershop", "salon"},
        "services": {"service", "services", "repair", "mechanic"},
        "shopping": {"shopping", "shop", "store", "retail", "market"},
        "tech": {"tech", "technology", "software", "computer", "electronics"},
        "entertainment": {"entertainment", "fun", "nightlife", "games", "gaming", "music"},
        "health": {"health", "healthcare", "hospital", "clinic", "pharmacy"},
        "education": {"education", "school", "training", "tutor"},
        "transport": {"transport", "transportation", "logistics", "delivery"},
    }
    category_map = {
        "food": {"food", "dining", "restaurant", "restaurants"},
        "hotels": {"hotel", "hotels", "lodging", "accommodation"},
        "fashion and beauty": {"fashion", "clothing", "beauty", "barber", "barbershop", "salon"},
        "services": {"service", "services", "repair", "mechanic"},
        "shopping": {"shopping", "shop", "store", "retail", "market"},
        "tech": {"tech", "technology", "software", "computer", "electronics"},
        "entertainment": {"entertainment", "fun", "nightlife", "games", "gaming", "music"},
        "health": {"health", "healthcare", "hospital", "clinic", "pharmacy"},
        "education": {"education", "school", "training", "tutor"},
        "transport": {"transport", "transportation", "logistics", "delivery"},
    }
    for group in groups:
        aliases = group_aliases.get(group, {group})
        if category in category_map and category_map[category].intersection(aliases):
            return True
        if any(re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", category_text) for alias in aliases):
            return True
    return False


@app.route("/businesses")
def businesses_page():
    search_query = request.args.get("q", "").strip()
    search_intent = _business_search_intent(search_query)
    selected_category = request.args.get("category", "all").strip().lower()
    selected_state_slug = request.args.get("state", "").strip().lower()

    # Do not silently restrict a search to the signed-in user's home state when
    # the query itself names a city/state or is a cross-location search.
    if "state" not in request.args and session.get("user_id") is not None and not (search_intent["city"] or search_intent["state"]):
        selected_state_slug = make_state_slug(get_user_home_state())

    selected_state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == selected_state_slug),
        None
    ) if selected_state_slug else None
    sort_by = request.args.get("sort", "featured").strip().lower()

    allowed_sorts = ["featured", "newest", "rating", "reviews", "location"]
    if sort_by not in allowed_sorts:
        sort_by = "featured"

    all_businesses = get_business_discovery_data()
    categories = get_business_categories()
    valid_category_slugs = [category_slug(category) for category in categories]

    if selected_category != "all" and selected_category not in valid_category_slugs:
        selected_category = "all"

    filtered_businesses = []
    search_lower = normalize_location(search_query)

    for business in all_businesses:
        if selected_state and not business_visible_in_state(business, selected_state):
            continue

        if not business_matches_category(business, selected_category):
            continue

        if search_intent["city"]:
            if get_business_city(business) != search_intent["city"]:
                continue
        elif search_intent["state"]:
            if get_business_state(business) != search_intent["state"]:
                # Allow an active premium listing to appear in a state it covers.
                if not (business.get("premium_listing") and str(business.get("premium_status", "")).upper() == "ACTIVE"
                        and any(normalize_location(item) == search_intent["state"] for item in business.get("premium_states", []))):
                    continue

        if search_lower:
            if not _business_matches_search_category(business, search_intent["groups"]):
                continue

            searchable_text = normalize_location(" ".join([
                str(business.get("name", "")),
                str(business.get("category", "")),
                str(business.get("service_type", "")),
                str(business.get("location", "")),
                str(business.get("description", "")),
                str(business.get("about", "")),
                str(business.get("city", "")),
                str(business.get("lga", "")),
                str(business.get("state", "")),
            ]))

            if any(keyword not in searchable_text for keyword in search_intent["keywords"]):
                continue

            # If no category, location or free-text keyword was parsed, retain
            # normal partial matching as a safe fallback.
            if not (search_intent["groups"] or search_intent["city"] or search_intent["state"] or search_intent["keywords"]):
                if search_lower not in searchable_text:
                    continue

        business["rating_label"] = get_rating_label(business)
        filtered_businesses.append(business)

    if sort_by == "rating":
        filtered_businesses.sort(
            key=lambda b: (
                b.get("rating_count", 0) > 0,
                b.get("rating_average", 0),
                b.get("rating_count", 0)
            ),
            reverse=True
        )
    elif sort_by == "reviews":
        filtered_businesses.sort(
            key=lambda b: (b.get("rating_count", 0), b.get("rating_average", 0)),
            reverse=True
        )
    elif sort_by == "location":
        filtered_businesses.sort(
            key=lambda b: b.get("location", "").replace("·", "").strip().lower()
        )
    elif sort_by == "newest":
        filtered_businesses.sort(key=lambda b: b.get("id", 0), reverse=True)
    else:
        filtered_businesses.sort(
            key=lambda b: (
                b.get("rating_count", 0) > 0,
                b.get("rating_average", 0),
                b.get("rating_count", 0),
                b.get("id", 0)
            ),
            reverse=True
        )

    category_options = [
        {"name": category, "slug": category_slug(category)}
        for category in categories
    ]

    return render_template(
        "businesses.html",
        businesses=filtered_businesses,
        categories=category_options,
        search_query=search_query,
        selected_category=selected_category,
        sort_by=sort_by,
        selected_state=selected_state,
        selected_state_slug=selected_state_slug,
        result_count=len(filtered_businesses),
        total_business_count=(
            sum(1 for business in all_businesses
                if business_visible_in_state(business, selected_state))
            if selected_state else len(all_businesses)
        )
    )


# =========================
# BUSINESS PROFILE
# =========================

@app.route("/business/<int:business_id>")
def business_profile(business_id):
    business = businesses.get(business_id)
    if business is None or not can_view_business(business):
        return "Business not found", 404

    current_user_id = session.get("user_id")
    existing_claim = None

    if current_user_id:
        existing_claim = next(
            (
                claim for claim in business_claims
                if claim["user_id"] == current_user_id
                and claim["business_id"] == business_id
            ),
            None
        )

    business_reviews = get_business_reviews(business_id)
    rating = get_business_rating(business_id)
    user_review = (
        get_user_business_review(business_id, current_user_id)
        if current_user_id else None
    )

    return render_template(
        "business.html",
        business=business,
        users=users,
        existing_claim=existing_claim,
        reviews=business_reviews,
        rating=rating,
        user_review=user_review,
        is_verified=is_business_verified(business_id),
        approved_claim=get_business_approved_claim(business_id),
        profile_picture_url=get_business_card_image(business),
        can_manage_profile=bool(current_user_id and business_has_access(business, current_user_id) and not business_management_blocked(business))
    )


@app.route("/manage/business/<int:business_id>/profile-picture", methods=["POST"])
def upload_business_profile_picture(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login", next=request.path))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None:
        return "Business not found", 404
    if not business_has_access(business, user_id):
        flash("You need approved business management access to change this public profile picture.", "error")
        return redirect(url_for("business_profile", business_id=business_id))
    if business_management_blocked(business):
        flash("Profile picture changes are paused while the ownership dispute is reviewed.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    upload = request.files.get("profile_picture")
    if not upload or not upload.filename:
        flash("Choose an image to upload.", "error")
        return redirect(url_for("business_profile", business_id=business_id))
    if not allowed_file(upload.filename):
        flash("Use a JPG, JPEG, PNG, or WEBP image.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size <= 0 or size > MAX_PHOTO_SIZE:
        flash("The profile picture must be under 5 MB.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    safe_name = secure_filename(upload.filename)
    if not safe_name or "." not in safe_name:
        flash("That image filename could not be processed.", "error")
        return redirect(url_for("business_profile", business_id=business_id))
    extension = safe_name.rsplit(".", 1)[1].lower()
    filename = f"profile_{business_id}_{uuid.uuid4().hex}.{extension}"
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    upload.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    old_picture = business.get("profile_picture")
    business["profile_picture"] = filename
    record_business_audit("BUSINESS_PROFILE_PICTURE_UPDATED", business_id, user_id, "Public profile picture replaced")
    save_app_state()

    # Remove only a prior locally uploaded profile image, never a gallery image or external URL.
    if isinstance(old_picture, str) and old_picture.startswith(f"profile_{business_id}_"):
        old_path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(old_picture))
        try:
            if os.path.isfile(old_path):
                os.remove(old_path)
        except OSError:
            app.logger.warning("Could not remove replaced profile picture: %s", old_path)

    flash("Business public profile picture updated.", "success")
    return redirect(url_for("business_profile", business_id=business_id))


@app.route("/manage/business/<int:business_id>/profile-picture/delete", methods=["POST"])
def delete_business_profile_picture(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login", next=request.path))
    business = businesses.get(business_id)
    if business is None:
        return "Business not found", 404
    if not business_has_access(business, user_id) or business_management_blocked(business):
        flash("You do not have permission to change this business profile picture.", "error")
        return redirect(url_for("business_profile", business_id=business_id))
    old_picture = business.pop("profile_picture", None)
    record_business_audit("BUSINESS_PROFILE_PICTURE_REMOVED", business_id, user_id, "Public profile picture removed")
    save_app_state()
    if isinstance(old_picture, str) and old_picture.startswith(f"profile_{business_id}_"):
        old_path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(old_picture))
        try:
            if os.path.isfile(old_path):
                os.remove(old_path)
        except OSError:
            app.logger.warning("Could not remove profile picture: %s", old_path)
    flash("Public profile picture removed. The business will use a gallery image or fallback instead.", "success")
    return redirect(url_for("business_profile", business_id=business_id))


@app.route("/business/<int:business_id>/review", methods=["POST"])
def submit_review(business_id):
    user_id = session.get("user_id")

    if user_id is None:
        flash("Please log in to leave a review.", "error")
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None or not can_view_business(business):
        return "Business not found", 404

    try:
        rating_value = int(request.form.get("rating", "").strip())
    except (TypeError, ValueError):
        flash("Please select a rating from 1 to 5 stars.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    review_text = request.form.get("review", "").strip()

    if rating_value < 1 or rating_value > 5:
        flash("Please select a rating from 1 to 5 stars.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    if not review_text:
        flash("Please write a short review.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    if len(review_text) > 1000:
        flash("Your review is too long. Please keep it under 1000 characters.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    existing_review = get_user_business_review(business_id, user_id)

    if existing_review:
        existing_review["rating"] = rating_value
        existing_review["review"] = review_text
        existing_review["updated_at"] = datetime.now().strftime("%b %d, %Y")
        flash("Your review has been updated.", "success")
    else:
        reviews.append({
            "id": max((item["id"] for item in reviews), default=0) + 1,
            "business_id": business_id,
            "user_id": user_id,
            "user_name": user["name"],
            "rating": rating_value,
            "review": review_text,
            "created_at": datetime.now().strftime("%b %d, %Y"),
            "updated_at": None
        })
        flash("Your review has been posted.", "success")

    return redirect(url_for("business_profile", business_id=business_id))


@app.route("/business/<int:business_id>/review/delete", methods=["POST"], endpoint="delete_business_review")
@app.route("/business/<int:business_id>/review/delete", methods=["POST"])
def delete_review(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None or not can_view_business(business):
        return "Business not found", 404

    review = get_user_business_review(business_id, user_id)
    if review is not None:
        reviews.remove(review)
        flash("Your review has been deleted.", "success")

    return redirect(url_for("business_profile", business_id=business_id))


# =========================
# NATIONWIDE SHOWCASE TRENDS
# =========================
# These trends are always Nigeria-wide (Google Trends geo=NG and Nigerian news
# feeds). They deliberately do not use the visitor's account/location setting.
NATIONAL_TRENDS_CACHE = {"updated_at": 0, "items": []}
NATIONAL_TRENDS_CACHE_TTL = 15 * 60
NATIONAL_TREND_FEEDS = [
    ("Google Trends Nigeria", "https://trends.google.com/trending/rss?geo=NG", "Google Trends"),
    ("Punch Newspapers", "https://punchng.com/feed/", "Nigerian news"),
    ("Channels Television", "https://www.channelstv.com/feed/", "Nigerian news"),
    ("Premium Times", "https://www.premiumtimesng.com/feed", "Nigerian news"),
]


def _fetch_national_trend_feed(feed):
    source, feed_url, source_type = feed
    request_obj = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "THE-ECOSYSTEM/1.0 (Nigeria trends reader; RSS)"}
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=2.5) as response:
            payload = response.read(900_000)
        root = ET.fromstring(payload)
    except Exception:
        return []

    entries = []
    # RSS and Atom both expose title/link/description, sometimes namespaced.
    for node in list(root.iter()):
        if node.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        fields = {}
        for child in list(node):
            key = child.tag.rsplit("}", 1)[-1].lower()
            if key == "link":
                value = child.attrib.get("href") or (child.text or "")
            else:
                value = " ".join("".join(child.itertext()).split())
            if value and key not in fields:
                fields[key] = value
        title = fields.get("title", "").strip()
        link = fields.get("link", "").strip()
        summary = fields.get("description") or fields.get("summary") or fields.get("content", "")
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = " ".join(summary.split())
        if not title or not link.startswith(("https://", "http://")):
            continue
        entries.append({
            "title": title[:180], "url": link, "summary": summary[:260],
            "source": source, "source_type": source_type,
        })
        if len(entries) >= 8:
            break
    return entries


def get_national_trends(force_refresh=False):
    """Return cached Nigeria-wide topics from public RSS feeds, without geolocation."""
    now = time.time()
    if (not force_refresh and NATIONAL_TRENDS_CACHE["items"]
            and now - NATIONAL_TRENDS_CACHE["updated_at"] < NATIONAL_TRENDS_CACHE_TTL):
        return NATIONAL_TRENDS_CACHE["items"]

    collected = []
    # Fetch feeds concurrently so a slow or unavailable provider doesn't stall the page.
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(NATIONAL_TREND_FEEDS)) as pool:
            futures = [pool.submit(_fetch_national_trend_feed, feed) for feed in NATIONAL_TREND_FEEDS]
            for future in concurrent.futures.as_completed(futures, timeout=3.2):
                try:
                    collected.extend(future.result())
                except Exception:
                    continue
    except Exception:
        pass

    seen = set()
    unique = []
    for item in collected:
        key = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        item["category"] = _categorize_national_trend(item)
        unique.append(item)
        if len(unique) >= 16:
            break

    # Preserve the last successful result if providers are temporarily unreachable.
    if unique:
        NATIONAL_TRENDS_CACHE["items"] = unique
        NATIONAL_TRENDS_CACHE["updated_at"] = now
    elif now - NATIONAL_TRENDS_CACHE["updated_at"] >= NATIONAL_TRENDS_CACHE_TTL:
        NATIONAL_TRENDS_CACHE["items"] = []
        NATIONAL_TRENDS_CACHE["updated_at"] = now
    return NATIONAL_TRENDS_CACHE["items"]


# =========================
# LIVE FOOTBALL + NATIONWIDE TREND CATEGORIES
# =========================
FOOTBALL_CACHE = {"updated_at": 0, "items": [], "configured": bool(os.getenv("API_FOOTBALL_KEY")), "error": None}
FOOTBALL_CACHE_TTL = 180


def _football_status_label(status):
    return {
        "1H": "1st Half", "HT": "Half-time", "2H": "2nd Half", "ET": "Extra Time",
        "P": "Penalties", "FT": "Full-time", "AET": "After Extra Time", "PEN": "Penalties",
        "NS": "Not started", "LIVE": "Live", "TBD": "Time TBC", "PST": "Postponed",
        "CANC": "Cancelled", "SUSP": "Suspended", "INT": "Interrupted",
    }.get(str(status or ""), str(status or "Scheduled"))


def get_live_football(force_refresh=False):
    """Fetch real live fixtures from API-Football; never invent scores."""
    now = time.time()
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    FOOTBALL_CACHE["configured"] = bool(api_key)
    if not api_key:
        return {"configured": False, "items": [], "updated_at": None, "error": "Add API_FOOTBALL_KEY to .env to enable live scores."}
    if not force_refresh and now - FOOTBALL_CACHE["updated_at"] < FOOTBALL_CACHE_TTL:
        return {"configured": True, "items": FOOTBALL_CACHE["items"], "updated_at": FOOTBALL_CACHE["updated_at"], "error": FOOTBALL_CACHE["error"]}
    req = urllib.request.Request(
        "https://v3.football.api-sports.io/fixtures?live=all",
        headers={"x-apisports-key": api_key, "User-Agent": "THE-ECOSYSTEM/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            payload = json.loads(response.read(1_500_000).decode("utf-8", "replace"))
        fixtures = []
        for item in payload.get("response", [])[:30]:
            fixture = item.get("fixture", {}) or {}
            teams = item.get("teams", {}) or {}
            goals = item.get("goals", {}) or {}
            league = item.get("league", {}) or {}
            status = (fixture.get("status", {}) or {}).get("short", "")
            fixtures.append({
                "id": fixture.get("id"), "home": (teams.get("home", {}) or {}).get("name", "Home"),
                "away": (teams.get("away", {}) or {}).get("name", "Away"),
                "home_logo": (teams.get("home", {}) or {}).get("logo", ""),
                "away_logo": (teams.get("away", {}) or {}).get("logo", ""),
                "home_goals": goals.get("home"), "away_goals": goals.get("away"),
                "minute": (fixture.get("status", {}) or {}).get("elapsed"),
                "status": _football_status_label(status), "league": league.get("name", "Football"),
                "country": league.get("country", ""), "is_live": status in {"1H", "HT", "2H", "ET", "P", "LIVE"},
            })
        FOOTBALL_CACHE.update({"updated_at": now, "items": fixtures, "error": None})
    except Exception:
        FOOTBALL_CACHE["error"] = "Live scores are temporarily unavailable. Please try again shortly."
    return {"configured": True, "items": FOOTBALL_CACHE["items"], "updated_at": FOOTBALL_CACHE["updated_at"], "error": FOOTBALL_CACHE["error"]}


def _categorize_national_trend(item):
    text = (str(item.get("title", "")) + " " + str(item.get("summary", ""))).lower()
    source = str(item.get("source", "")).lower()
    if any(word in text for word in ("football", "premier league", "super eagles", "chelsea", "arsenal", "manchester", "liverpool", "champions league", "afcon", "transfer news", "goal scored")):
        return "football"
    if any(word in text for word in ("fashion", "style", "beauty", "designer", "runway", "lifestyle")):
        return "fashion"
    if any(word in text for word in ("food", "recipe", "restaurant", "cuisine", "festival", "culture")):
        return "food"
    if any(word in text for word in ("government", "president", "senate", "politic", "minister", "election", "assembly", "policy")):
        return "politics"
    if any(word in text for word in ("music", "artist", "album", "singer", "afrobeats", "nollywood", "movie", "celebrity", "entertainment")):
        return "entertainment"
    if any(word in text for word in ("business", "startup", "technology", "tech", "ai ", "innovation", "economy", "naira", "investment", "company")):
        return "business-tech"
    return "nigeria"


# =========================
# P8 — BUSINESS SHOWCASE
# =========================

def get_showcase_post(post_id, business_id=None):
    return next((post for post in showcase_posts
                 if str(post.get("id")) == str(post_id)
                 and (business_id is None or str(post.get("business_id")) == str(business_id))), None)


def showcase_activity_for(post_id, business_id, action=None):
    return [item for item in showcase_activity
            if str(item.get("post_id")) == str(post_id)
            and str(item.get("business_id")) == str(business_id)
            and (action is None or str(item.get("action", "")).lower() == action)]


def _showcase_actor_name(user_id):
    user = get_user_by_id(user_id) if user_id is not None else None
    if not user:
        return "Community member"
    return str(user.get("name") or user.get("full_name") or user.get("username") or user.get("email") or "Community member")[:80]


@app.route("/showcase", methods=["GET"])
def showcase_feed():
    """Public Showcase feed showing posts from all visible businesses."""
    current_user_id = session.get("user_id")
    feed_posts = []
    for saved_post in showcase_posts:
        business_id = saved_post.get("business_id")
        try:
            business_id = int(business_id)
        except (TypeError, ValueError):
            continue
        business = businesses.get(business_id)
        if not business or not can_view_business(business):
            continue

        post = saved_post.copy()
        post["business"] = business
        if not post.get("media") and post.get("image_url"):
            post["media"] = [{"type": "image", "url": post["image_url"]}]
        post["media"] = post.get("media") or []
        post["likes_count"] = sum(
            1 for item in showcase_activity_for(post.get("id"), business_id, "like")
            if not item.get("removed")
        )
        post["liked_by_me"] = bool(current_user_id is not None and any(
            str(item.get("user_id")) == str(current_user_id) and not item.get("removed")
            for item in showcase_activity_for(post.get("id"), business_id, "like")
        ))
        post["comments"] = sorted(
            [item.copy() for item in showcase_activity_for(post.get("id"), business_id, "comment")
             if not item.get("hidden")],
            key=lambda item: item.get("created_at", "")
        )
        feed_posts.append(post)

    feed_posts.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return render_template(
        "showcase_feed.html", posts=feed_posts,
        is_logged_in=current_user_id is not None,
        current_user_id=current_user_id,
        national_trends=get_national_trends(),
        live_football=get_live_football()
    )


@app.route("/business/<int:business_id>/showcase", methods=["GET", "POST"])
def business_showcase(business_id):
    business = businesses.get(business_id)
    if business is None or not can_view_business(business):
        return "Business not found", 404

    display_name = (business.get("name") or business.get("business_name") or business.get("company") or business.get("title") or "This business")
    business["name"] = str(display_name).strip() or "This business"
    current_user_id = session.get("user_id")
    is_owner = business_has_access(business, current_user_id) or str(business.get("primary_owner_id", "")) == str(current_user_id)

    if request.method == "POST":
        if not is_owner:
            flash("Only this business's owner or approved manager can publish Showcase posts.", "error")
            return redirect(url_for("business_showcase", business_id=business_id))

        caption = (request.form.get("caption") or "").strip()
        uploaded_media = [item for item in request.files.getlist("media") if item and item.filename]
        # Accept the previous single-image field too, so old forms/integrations keep working.
        legacy_image = request.files.get("image")
        if legacy_image and legacy_image.filename:
            uploaded_media.append(legacy_image)

        if len(uploaded_media) > 12:
            flash("You can upload up to 12 photos and videos in one Showcase post.", "error")
            return redirect(url_for("business_showcase", business_id=business_id))
        if not caption and not uploaded_media:
            flash("Add a caption or choose at least one photo or video before publishing.", "error")
            return redirect(url_for("business_showcase", business_id=business_id))

        image_exts = {"jpg", "jpeg", "png", "webp"}
        video_exts = {"mp4", "webm", "mov"}
        validated = []
        image_count = video_count = 0
        for item in uploaded_media:
            filename = secure_filename(item.filename)
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext in image_exts:
                image_count += 1
                limit = MAX_PHOTO_SIZE
                media_type = "image"
            elif ext in video_exts:
                video_count += 1
                limit = MAX_PREMIUM_VIDEO_SIZE
                media_type = "video"
            else:
                flash("Supported files: JPG, PNG, WEBP images and MP4, WEBM, MOV videos.", "error")
                return redirect(url_for("business_showcase", business_id=business_id))
            item.seek(0, os.SEEK_END)
            file_size = item.tell()
            item.seek(0)
            if file_size > limit:
                size_label = "5 MB" if media_type == "image" else "40 MB"
                flash(f"Each Showcase {media_type} must be {size_label} or smaller.", "error")
                return redirect(url_for("business_showcase", business_id=business_id))
            validated.append((item, ext, media_type))

        if image_count > 8 or video_count > 4:
            flash("Each post can contain up to 8 photos and 4 videos (12 files total).", "error")
            return redirect(url_for("business_showcase", business_id=business_id))

        media_items = []
        os.makedirs(SHOWCASE_UPLOAD_FOLDER, exist_ok=True)
        for item, ext, media_type in validated:
            safe_name = f"showcase_{business_id}_{secrets.token_hex(8)}.{ext}"
            item.save(os.path.join(SHOWCASE_UPLOAD_FOLDER, safe_name))
            media_items.append({
                "type": media_type,
                "url": url_for("static", filename=f"uploads/showcase/{safe_name}"),
                "filename": safe_name,
            })

        post_id = max((int(p.get("id", 0)) for p in showcase_posts), default=0) + 1
        showcase_posts.append({
            "id": post_id, "business_id": business_id, "caption": caption[:2000],
            "image_url": next((m["url"] for m in media_items if m["type"] == "image"), ""),
            "media": media_items, "user_id": current_user_id,
            "author": _showcase_actor_name(current_user_id),
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        save_app_state()
        flash("Your Showcase post is live.", "success")
        return redirect(url_for("business_showcase", business_id=business_id))

    posts = sorted([p for p in showcase_posts if str(p.get("business_id")) == str(business_id)],
                   key=lambda p: p.get("created_at", ""), reverse=True)
    for post in posts:
        # Upgrade legacy single-image posts to the same media-gallery shape at render time.
        if not post.get("media") and post.get("image_url"):
            post["media"] = [{"type": "image", "url": post["image_url"]}]
        post["media"] = post.get("media") or []
        post["likes_count"] = sum(1 for item in showcase_activity_for(post["id"], business_id, "like") if not item.get("removed"))
        post["comments"] = sorted([item for item in showcase_activity_for(post["id"], business_id, "comment") if not item.get("hidden")], key=lambda x: x.get("created_at", ""))
        post["liked_by_me"] = any(str(item.get("user_id")) == str(current_user_id) and not item.get("removed") for item in showcase_activity_for(post["id"], business_id, "like")) if current_user_id is not None else False
        # Count a view once per browser session per post to avoid refresh inflation.
        viewed = session.get("showcase_viewed_posts", [])
        view_key = f"{business_id}:{post['id']}"
        if view_key not in viewed:
            showcase_activity.append({"id": secrets.token_hex(10), "business_id": business_id, "post_id": post["id"],
                                     "action": "view", "user_id": current_user_id,
                                     "created_at": datetime.now(timezone.utc).isoformat()})
            session["showcase_viewed_posts"] = (viewed + [view_key])[-300:]
    return render_template("showcase.html", business=business, posts=posts, is_owner=is_owner,
                           current_user_id=current_user_id, is_logged_in=current_user_id is not None)


@app.post("/business/<int:business_id>/showcase/post/<int:post_id>/like")
def showcase_like(business_id, post_id):
    business = businesses.get(business_id)
    post = get_showcase_post(post_id, business_id)
    user_id = session.get("user_id")
    if not business or not post or not can_view_business(business):
        return jsonify({"ok": False, "error": "Post not found."}), 404
    if user_id is None:
        return jsonify({"ok": False, "error": "Log in to like Showcase posts.", "login_url": url_for("login")}), 401
    existing = next((item for item in showcase_activity_for(post_id, business_id, "like")
                     if str(item.get("user_id")) == str(user_id) and not item.get("removed")), None)
    if existing:
        showcase_activity.remove(existing)
        liked = False
    else:
        showcase_activity.append({"id": secrets.token_hex(10), "business_id": business_id, "post_id": post_id,
                                  "action": "like", "user_id": user_id, "created_at": datetime.now(timezone.utc).isoformat()})
        liked = True
    save_app_state()
    return jsonify({"ok": True, "liked": liked, "count": sum(1 for item in showcase_activity_for(post_id, business_id, "like") if not item.get("removed"))})


@app.post("/business/<int:business_id>/showcase/post/<int:post_id>/comment")
def showcase_comment(business_id, post_id):
    business = businesses.get(business_id)
    post = get_showcase_post(post_id, business_id)
    user_id = session.get("user_id")
    if not business or not post or not can_view_business(business):
        return "Post not found", 404
    if user_id is None:
        flash("Please log in to comment on Showcase posts.", "error")
        return redirect(url_for("login"))
    content = (request.form.get("comment") or "").strip()
    if not content or len(content) > 1000:
        flash("Comments must contain 1–1000 characters.", "error")
        return redirect(url_for("business_showcase", business_id=business_id) + f"#post-{post_id}")
    showcase_activity.append({"id": secrets.token_hex(10), "business_id": business_id, "post_id": post_id,
                              "action": "comment", "user_id": user_id, "author": _showcase_actor_name(user_id),
                              "content": content, "reply": "", "hidden": False,
                              "created_at": datetime.now(timezone.utc).isoformat()})
    save_app_state()
    return redirect(url_for("business_showcase", business_id=business_id) + f"#post-{post_id}")


@app.post("/business/<int:business_id>/showcase/comment/<comment_id>/reply")
def showcase_reply(business_id, comment_id):
    business = businesses.get(business_id)
    if not business or not (business_has_access(business, session.get("user_id")) or str(business.get("primary_owner_id", "")) == str(session.get("user_id"))):
        return "Business owner access required", 403
    comment = next((item for item in showcase_activity if str(item.get("id")) == str(comment_id)
                    and str(item.get("business_id")) == str(business_id) and item.get("action") == "comment"), None)
    if not comment:
        return "Comment not found", 404
    reply = (request.form.get("reply") or "").strip()
    if not reply or len(reply) > 1000:
        flash("Replies must contain 1–1000 characters.", "error")
    else:
        comment["reply"] = reply
        comment["reply_by"] = _showcase_actor_name(session.get("user_id"))
        comment["replied_at"] = datetime.now(timezone.utc).isoformat()
        save_app_state()
        flash("Reply posted.", "success")
    return redirect(url_for("business_showcase", business_id=business_id) + f"#post-{comment.get('post_id')}")


@app.post("/business/<int:business_id>/showcase/post/<int:post_id>/share")
def showcase_share(business_id, post_id):
    business = businesses.get(business_id)
    post = get_showcase_post(post_id, business_id)
    if not business or not post or not can_view_business(business):
        return jsonify({"ok": False, "error": "Post not found."}), 404
    showcase_activity.append({"id": secrets.token_hex(10), "business_id": business_id, "post_id": post_id,
                              "action": "share", "user_id": session.get("user_id"),
                              "created_at": datetime.now(timezone.utc).isoformat()})
    save_app_state()
    return jsonify({"ok": True, "url": request.url_root.rstrip("/") + url_for("business_showcase", business_id=business_id) + f"#post-{post_id}"})


# =========================
# P9 — COMPETITIONS LIST
# =========================

def get_published_award_winners(user_id=None):
    """Build award results from approved entries after voting has closed."""
    vote_counts = {}
    for vote in competition_votes:
        key = (str(vote.get("competition_id")), str(vote.get("entry_id")))
        vote_counts[key] = vote_counts.get(key, 0) + 1

    grouped = {}
    for entry in competition_entries:
        if str(entry.get("status", "")).upper() != "APPROVED":
            continue
        competition = get_competition_by_id(entry.get("competition_id"))
        if not competition or str(competition.get("voting_status", "CLOSED")).upper() != "CLOSED":
            continue
        votes = vote_counts.get((str(entry.get("competition_id")), str(entry.get("id"))), 0)
        if votes <= 0:
            continue
        state = entry.get("state") or "Nigeria"
        key = (str(entry.get("competition_id")), normalize_location(state))
        grouped.setdefault(key, []).append((entry, votes, competition))

    winners = []
    for nominees in grouped.values():
        highest = max(votes for _, votes, _ in nominees)
        for entry, votes, competition in nominees:
            if votes != highest:
                continue
            if user_id is not None and str(entry.get("user_id")) != str(user_id):
                continue
            winners.append({"entry": entry, "competition": competition, "votes": votes,
                            "state": entry.get("state") or "Nigeria"})
    return sorted(winners, key=lambda item: (
        normalize_location(item["state"]),
        str(item["competition"].get("title", "")).lower(),
        str(item["entry"].get("entrant_name", "")).lower(),
    ))


@app.route("/awards")
def awards():
    """Public awards page showing winners for categories with closed voting."""
    return render_template("awards.html", winners=get_published_award_winners(), personal=False)


@app.route("/account/awards")
def account_awards():
    """Account-specific award and certificate library."""
    user_id = session.get("user_id")
    if user_id is None or get_user_by_id(user_id) is None:
        return redirect(url_for("login", next=request.path))
    return render_template("awards.html", winners=get_published_award_winners(user_id=user_id), personal=True)


@app.route("/awards/certificate/<int:entry_id>")
def award_certificate(entry_id):
    """Print-ready certificate for an award attached to the signed-in user's account."""
    user_id = session.get("user_id")
    if user_id is None or get_user_by_id(user_id) is None:
        return redirect(url_for("login", next=request.path))
    winner = next((item for item in get_published_award_winners(user_id=user_id)
                   if int(item["entry"].get("id", -1)) == entry_id), None)
    if winner is None:
        return "Certificate not found or you do not have access to it.", 404
    return render_template("award_certificate.html", winner=winner, user=get_user_by_id(user_id))


@app.route("/competitions")
def competitions():
    # Competition copy and descriptions follow the state saved in the user's account.
    current_user = get_user_by_id(session.get("user_id")) if session.get("user_id") is not None else None
    selected_state = get_user_home_state(current_user) if current_user else "Oyo"
    competition_cards = get_competitions_for_state(selected_state)

    for competition_copy in competition_cards:
        competition_copy["approved_entry_count"] = len(
            get_approved_competition_entries_for_state(competition_copy["id"], selected_state)
        )

    display_state = "Abuja" if normalize_location(selected_state) == "federal capital territory" else selected_state
    return render_template(
        "competitions.html", competitions=competition_cards,
        selected_state=selected_state, selected_state_label=display_state
    )


# =========================
# P9 — COMPETITION DETAIL
# =========================

@app.route("/competition/<int:competition_id>")
def competition_detail(competition_id):
    competition = get_competition_by_id(competition_id)
    if competition is None:
        return "Competition not found", 404

    user_id = session.get("user_id")
    user = get_user_by_id(user_id) if user_id is not None else None
    requested_state = request.args.get("state", "").strip()
    if user:
        # The saved account state is authoritative for logged-in voters.
        competition_state = get_user_home_state(user)
    elif requested_state:
        normalized_requested_state = normalize_location(requested_state)
        competition_state = next(
            (state for state in NIGERIA_STATES if normalize_location(state) == normalized_requested_state),
            None,
        )
        if normalized_requested_state in {"abuja", "abj", "fct", "federal capital", "federal capital territory"}:
            competition_state = "Federal Capital Territory"
        competition_state = competition_state or "Oyo"
    else:
        competition_state = "Oyo"

    competition = next(
        (item for item in get_competitions_for_state(competition_state) if item.get("id") == competition_id),
        competition,
    )
    approved_entries = get_approved_competition_entries_for_state(competition_id, competition_state)

    user_entries = (
        get_user_competition_entries(competition_id, user_id)
        if user_id is not None else []
    )

    user_vote = (
        get_user_competition_vote(competition_id, user_id)
        if user_id is not None else None
    )

    return render_template(
        "competition.html",
        competition=competition,
        competition_id=competition_id,
        state=competition_state,
        state_slug=make_state_slug(competition_state),
        approved_entries=approved_entries,
        user_entries=user_entries,
        can_enter=(
            user_id is not None
            and competition.get("status") != "CLOSED"
        ),
        voting_open=competition.get("voting_status", "CLOSED") == "OPEN",
        user_vote_entry_id=user_vote["entry_id"] if user_vote else None
    )


# =========================
# P9 — COMPETITION VOTING
# =========================

@app.route(
    "/competition/<int:competition_id>/vote/<int:entry_id>",
    methods=["POST"]
)
def vote_competition_entry(competition_id, entry_id):
    user_id = session.get("user_id")

    if user_id is None:
        flash("Please log in before voting.", "error")
        return redirect(url_for("login", next=request.path))

    if get_user_by_id(user_id) is None:
        session.pop("user_id", None)
        flash("Please log in again before voting.", "error")
        return redirect(url_for("login"))

    competition = get_competition_by_id(competition_id)
    if competition is None:
        return "Competition not found", 404

    if competition.get("voting_status", "CLOSED") != "OPEN":
        flash("Voting is not currently open for this competition.", "error")
        return redirect(url_for("competition_detail", competition_id=competition_id))

    entry = get_competition_entry_by_id(entry_id)

    if (
        entry is None
        or entry.get("competition_id") != competition_id
        or entry.get("status") != "APPROVED"
    ):
        flash("This nominee is not eligible for voting.", "error")
        return redirect(url_for("competition_detail", competition_id=competition_id))

    voter = get_user_by_id(user_id)
    voter_state = get_user_home_state(voter)
    if normalize_location(get_competition_entry_state(entry)) != normalize_location(voter_state):
        flash(f"You can only vote for nominees in {voter_state}.", "error")
        return redirect(url_for("competition_detail", competition_id=competition_id))

    existing_vote = get_user_competition_vote(competition_id, user_id)
    if existing_vote:
        if existing_vote.get("entry_id") == entry_id:
            flash("Your vote has already been counted for this nominee.", "info")
        else:
            flash("You have already voted in this category. Each voter gets one vote per category, and votes cannot be changed.", "error")
        return redirect(url_for("competition_detail", competition_id=competition_id, state=voter_state))

    competition_votes.append({
        "id": max((vote.get("id", 0) for vote in competition_votes), default=0) + 1,
        "competition_id": competition_id,
        "entry_id": entry_id,
        "user_id": user_id,
        "voted_at": datetime.now().strftime("%b %d, %Y %H:%M")
    })
    flash("Your vote has been counted! You cannot vote again in this category.", "success")
    return redirect(url_for("competition_detail", competition_id=competition_id, state=voter_state))


# =========================
# P9 — ENTER / NOMINATE
# =========================

@app.route("/competition/<int:competition_id>/enter", methods=["GET", "POST"])
def competition_entry(competition_id):
    competition = get_competition_by_id(competition_id)
    if competition is None:
        return "Competition not found", 404

    user_id = session.get("user_id")

    if user_id is None:
        flash("Please log in or create an account before submitting a competition entry.", "error")
        return redirect(url_for("login", next=request.path))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    competition = next(
        (item for item in get_competitions_for_state(get_user_home_state(user)) if item.get("id") == competition_id),
        competition,
    )

    if competition.get("status") == "CLOSED":
        flash("Entries for this competition are closed.", "error")
        return redirect(url_for("competition_detail", competition_id=competition_id))

    existing_entries = get_user_competition_entries(competition_id, user_id)

    if request.method == "POST":
        entrant_name = request.form.get("entrant_name", "").strip()
        entrant_type = request.form.get("entrant_type", "business").strip().lower()
        description = request.form.get("description", "").strip()
        reason = request.form.get("reason", "").strip()
        contact = request.form.get("contact", "").strip()
        supporting_link = request.form.get("supporting_link", "").strip()
        business_id_raw = request.form.get("business_id", "").strip()

        if not entrant_name or not description or not reason:
            return render_template(
                "competition_entry.html",
                competition=competition,
                user=user,
                existing_entries=existing_entries,
                error="Please provide the entrant name, description, and reason for nomination."
            )

        if len(entrant_name) > 120:
            return render_template(
                "competition_entry.html",
                competition=competition,
                user=user,
                existing_entries=existing_entries,
                error="The entrant name must be 120 characters or fewer."
            )

        if len(description) > 1500 or len(reason) > 1500:
            return render_template(
                "competition_entry.html",
                competition=competition,
                user=user,
                existing_entries=existing_entries,
                error="Descriptions and nomination reasons must be 1,500 characters or fewer."
            )

        if entrant_type not in ("business", "creator", "individual", "startup", "other"):
            entrant_type = "other"

        business_id = None
        if business_id_raw:
            try:
                possible_business_id = int(business_id_raw)
            except ValueError:
                possible_business_id = None

            if possible_business_id in businesses:
                business_id = possible_business_id

        nominee_state = get_user_home_state(user)
        if business_id is not None:
            linked_business_state = get_business_state(businesses[business_id])
            if linked_business_state and normalize_location(linked_business_state) != normalize_location(nominee_state):
                return render_template(
                    "competition_entry.html",
                    competition=competition,
                    user=user,
                    existing_entries=existing_entries,
                    error=f"Nominees must belong to your selected state ({nominee_state}). Choose a business from that state."
                )
            if linked_business_state:
                nominee_state = next(
                    (state for state in NIGERIA_STATES if normalize_location(state) == linked_business_state),
                    nominee_state,
                )

        entry = {
            "id": max((item.get("id", 0) for item in competition_entries), default=0) + 1,
            "competition_id": competition_id,
            "competition_title": competition["title"],
            "user_id": user_id,
            "submitter_name": user.get("name", "Community member"),
            "entrant_name": entrant_name,
            "entrant_type": entrant_type,
            "state": nominee_state,
            "business_id": business_id,
            "description": description,
            "reason": reason,
            "contact": contact,
            "supporting_link": supporting_link,
            "status": "PENDING",
            "votes": 0,
            "submitted_at": datetime.now().strftime("%b %d, %Y")
        }

        competition_entries.append(entry)
        notify_admin("Competition entry awaiting review", f"{entry.get('submitter_name')} submitted {entrant_name} for {competition.get('title')}.", "competition_entry", entry.get("id"))
        save_app_state()
        flash("Your entry has been submitted and is awaiting review.", "success")
        return redirect(url_for("competition_detail", competition_id=competition_id))

    return render_template(
        "competition_entry.html",
        competition=competition,
        user=user,
        existing_entries=existing_entries
    )


@app.route("/competition/<int:competition_id>/entries")
def competition_entries_page(competition_id):
    competition = get_competition_by_id(competition_id)
    if competition is None:
        return "Competition not found", 404

    user_id = session.get("user_id")
    user = get_user_by_id(user_id) if user_id is not None else None
    requested_state = request.args.get("state", "").strip()
    if requested_state:
        normalized_requested_state = normalize_location(requested_state)
        selected_state = next(
            (state for state in NIGERIA_STATES if normalize_location(state) == normalized_requested_state),
            None,
        )
        if normalized_requested_state in {"abuja", "abj", "fct", "federal capital", "federal capital territory"}:
            selected_state = "Federal Capital Territory"
    else:
        selected_state = get_user_home_state(user) if user else "Oyo"

    competition = next(
        (item for item in get_competitions_for_state(selected_state) if item.get("id") == competition_id),
        competition,
    )

    return render_template(
        "competition_entries.html",
        competition=competition,
        state=selected_state,
        state_slug=make_state_slug(selected_state),
        entries=get_approved_competition_entries_for_state(competition_id, selected_state)
    )


# =========================
# P10 — EVENTS
# =========================

def event_is_expired(event):
    """Determine whether a submitted event date has passed."""
    raw_date = str(event.get("event_date") or "").strip()
    parsed = None
    if raw_date:
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            parsed = None
    if parsed is None:
        display_date = str(event.get("date") or "").strip().title()
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                parsed = datetime.strptime(display_date, fmt).date()
                break
            except ValueError:
                continue
    return parsed is not None and parsed < datetime.now().date()


def refresh_expired_event_statuses():
    """Mark approved submitted events as expired after their date."""
    changed = False
    for event in event_submissions:
        if event.get("status") == "APPROVED" and event_is_expired(event):
            event["status"] = "EXPIRED"
            event["expired_at"] = datetime.now().strftime("%b %d, %Y")
            changed = True
    if changed:
        save_app_state()
    return changed


EVENTS_DATA = [
    {
        "id": 1, "title": "Ibadan Tech Summit", "category": "TECH",
        "icon": "💻", "date": "OCT 24, 2026", "time": "10:00 AM",
        "location": "SAMONDA · IBADAN",
        "description": "A gathering of developers, founders, innovators and technology enthusiasts building the future from Ibadan.",
        "status": "UPCOMING", "host": "Tech Hub Ibadan",
        "about": "Ibadan Tech Summit brings together people building with technology across Ibadan. The event creates a space for developers, founders, students, innovators and technology enthusiasts to connect, share ideas and discover what is being built locally."
    },
    {
        "id": 2, "title": "Ibadan Food Festival", "category": "FOOD",
        "icon": "🍽️", "date": "NOV 07, 2026", "time": "12:00 PM",
        "location": "BODIJA · IBADAN",
        "description": "A celebration of food, local restaurants, chefs and the flavours that make Ibadan unique.",
        "status": "UPCOMING", "host": "Local Bites",
        "about": "Ibadan Food Festival brings restaurants, food businesses, chefs and food lovers together for a day centred around the flavours and experiences that make the city special."
    },
    {
        "id": 3, "title": "Ibadan Fashion Weekend", "category": "FASHION",
        "icon": "👗", "date": "NOV 21, 2026", "time": "2:00 PM",
        "location": "BODIJA · IBADAN",
        "description": "A showcase of fashion, designers, models and creative talent from across Ibadan.",
        "status": "UPCOMING", "host": "House of Style",
        "about": "Ibadan Fashion Weekend brings together designers, models, stylists, photographers and fashion lovers to celebrate the growing creative fashion scene in Ibadan."
    },
    {
        "id": 4, "title": "The Ibadan Creative Meetup", "category": "CREATORS",
        "icon": "🎨", "date": "DEC 05, 2026", "time": "3:00 PM",
        "location": "SAMONDA · IBADAN",
        "description": "Creators, photographers, designers, filmmakers and other creatives connecting and collaborating.",
        "status": "UPCOMING", "host": "The Hangout",
        "about": "The Ibadan Creative Meetup is designed to bring local creatives together, create new connections and encourage collaboration across different creative fields."
    },
    {
        "id": 5, "title": "Ibadan Startup Demo Day", "category": "STARTUPS",
        "icon": "🚀", "date": "DEC 12, 2026", "time": "11:00 AM",
        "location": "SAMONDA · IBADAN",
        "description": "Local startups present their ideas, products and innovations to the Ibadan community.",
        "status": "UPCOMING", "host": "Tech Hub Ibadan",
        "about": "Ibadan Startup Demo Day gives local founders and startups an opportunity to showcase what they are building, meet potential collaborators and introduce new ideas to the community."
    },
    {
        "id": 6, "title": "Ibadan Community Game Night", "category": "COMMUNITY",
        "icon": "🎮", "date": "DEC 19, 2026", "time": "6:00 PM",
        "location": "SAMONDA · IBADAN",
        "description": "Games, music, food and good vibes bringing the local community together.",
        "status": "UPCOMING", "host": "The Hangout",
        "about": "A relaxed community event built around games, music, food and meeting new people. It is another way for people across Ibadan to connect offline."
    }
]


@app.route("/events")
def events():
    # An explicitly selected, valid state takes priority. If no valid state was
    # selected, use the logged-in user's registered residence (Oyo for guests
    # and legacy accounts without a saved residence).
    requested_state_slug = request.args.get("state", "").strip().lower()
    requested_state = next(
        (
            name for name in NIGERIA_STATES
            if make_state_slug(name) == requested_state_slug
        ),
        None,
    ) if requested_state_slug else None

    selected_state = requested_state or get_user_home_state()

    refresh_expired_event_statuses()
    all_visible_events = list(EVENTS_DATA) + [
        event for event in event_submissions
        if str(event.get("status", "")).strip().upper() == "APPROVED"
        and not event_is_expired(event)
    ]

    def event_belongs_to_state(event, state_name):
        target_state = normalize_location(state_name)
        # Approved, paid Premium campaigns can make an event discoverable in
        # additional purchased states while preserving its original event state.
        if str(event.get("premium_status", "")).upper() == "ACTIVE":
            premium_states = event.get("premium_states") or []
            if any(normalize_location(item) == target_state for item in premium_states):
                return True

        # Submitted events store state directly. Always trust a valid explicit
        # state field over a city/venue string.
        event_state = str(event.get("state", "") or "").strip()
        if event_state:
            normalized_event_state = normalize_location(event_state)
            canonical_event_state = next(
                (
                    name for name in NIGERIA_STATES
                    if normalize_location(name) == normalized_event_state
                ),
                None,
            )
            if canonical_event_state:
                return normalize_location(canonical_event_state) == target_state

        # Legacy/static events often only have a location such as
        # "BODIJA · IBADAN". Infer state from the known city where possible.
        inferred_state = get_business_state(event)
        if inferred_state:
            return normalize_location(inferred_state) == target_state

        # Do not show an event with unknown location in every state.
        return False

    visible_events = [
        event for event in all_visible_events
        if event_belongs_to_state(event, selected_state)
    ]

    return render_template(
        "events.html",
        events=visible_events,
        state=selected_state,
        state_slug=make_state_slug(selected_state),
        states=NIGERIA_STATES,
    )


@app.route("/event/<int:event_id>")
def event_detail(event_id):
    event = next((item for item in EVENTS_DATA if item["id"] == event_id), None)
    if event is None:
        refresh_expired_event_statuses()
        event = next(
            (item for item in event_submissions
             if item.get("id") == event_id
             and item.get("status") == "APPROVED"
             and not event_is_expired(item)),
            None,
        )
    if event is None:
        return "Event not found", 404

    # Keep the visitor's selected state when returning to the events directory.
    requested_state_slug = request.args.get("state", "").strip().lower()
    requested_state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == requested_state_slug),
        None,
    ) if requested_state_slug else None
    event_state = event.get("state") or get_business_state(event)
    if event_state:
        canonical_event_state = next(
            (name for name in NIGERIA_STATES if normalize_location(name) == normalize_location(event_state)),
            None,
        )
    else:
        canonical_event_state = None
    selected_state = requested_state or canonical_event_state or get_user_home_state()

    return render_template(
        "event.html", event=event, event_id=event_id,
        state=selected_state, state_slug=make_state_slug(selected_state),
    )


# =========================
# P11 — CREATORS
# =========================

creators = [
    {
        "id": 1, "name": "Tobi Creates", "category": "CONTENT",
        "location": "BODIJA · IBADAN", "icon": "🎨",
        "description": "Digital creator focused on lifestyle, culture and the everyday Ibadan experience.",
        "about": "Tobi Creates documents local experiences and gives people a fresh perspective on what's happening around Ibadan."
    },
    {
        "id": 2, "name": "Ayo Visuals", "category": "PHOTOGRAPHY",
        "location": "SAMONDA · IBADAN", "icon": "📸",
        "description": "Visual storyteller capturing people, places and moments across Ibadan.",
        "about": "Ayo Visuals focuses on photography, portraits and visual storytelling with a strong local identity."
    },
    {
        "id": 3, "name": "Mide Films", "category": "FILM",
        "location": "RING ROAD · IBADAN", "icon": "🎬",
        "description": "Independent filmmaker creating short films and visual stories.",
        "about": "Mide Films explores Nigerian stories through short films, creative direction and cinematic content."
    },
    {
        "id": 4, "name": "Kemi Styles", "category": "FASHION",
        "location": "BODIJA · IBADAN", "icon": "👗",
        "description": "Fashion creator blending modern Nigerian style with local culture.",
        "about": "Kemi Styles creates fashion content, styling concepts and collaborations with local brands."
    },
    {
        "id": 5, "name": "DJ Rhyme", "category": "MUSIC",
        "location": "DUGBE · IBADAN", "icon": "🎵",
        "description": "DJ and music curator bringing different sounds together across Ibadan.",
        "about": "DJ Rhyme creates mixes, performs at events and collaborates with artists and entertainment brands."
    },
    {
        "id": 6, "name": "Code With Femi", "category": "TECH",
        "location": "SAMONDA · IBADAN", "icon": "💻",
        "description": "Tech creator sharing software, innovation and technology projects.",
        "about": "Code With Femi creates educational technology content and highlights the growing tech ecosystem in Ibadan."
    }
]


@app.route("/creators")
def creators_page():
    selected_state_slug = request.args.get("state", "").strip().lower()
    if "state" not in request.args and session.get("user_id") is not None:
        selected_state_slug = make_state_slug(get_user_home_state())
    selected_state = next(
        (name for name in NIGERIA_STATES if make_state_slug(name) == selected_state_slug),
        None,
    ) if selected_state_slug else None

    visible_creators = list(creators)
    if selected_state:
        visible_creators = [
            creator for creator in visible_creators
            if get_business_state(creator) == normalize_location(selected_state)
        ]

    return render_template(
        "creators.html",
        creators=visible_creators,
        state=selected_state,
        state_slug=make_state_slug(selected_state) if selected_state else "",
    )


@app.route("/creator/<int:creator_id>")
def creator_profile(creator_id):
    creator = next((item for item in creators if item["id"] == creator_id), None)
    if creator is None:
        return "Creator not found", 404
    return render_template("creator.html", creator=creator)


# =========================
# P12.1 — REGISTER
# =========================


@app.route("/register", methods=["GET", "POST"])
def register():
    def registration_page(**context):
        context.setdefault("states", NIGERIA_STATES)
        return render_template("register.html", **context)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        state_of_residence = request.form.get("state_of_residence", "").strip()
        city_of_residence = request.form.get("city_of_residence", "").strip()
        lga_of_residence = request.form.get("lga_of_residence", "").strip()

        form_context = {
            "name": name,
            "email": email,
            "selected_state": state_of_residence,
            "selected_city": city_of_residence,
            "selected_lga": lga_of_residence,
        }

        if not name or not email or not password:
            return registration_page(
                **form_context, error="Please fill in all required fields."
            )

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return registration_page(
                **form_context, error="Please enter a valid email address."
            )

        if len(password) < 8:
            return registration_page(
                **form_context,
                error="Password must contain at least 8 characters."
            )

        selected_state = next(
            (state for state in NIGERIA_STATES
             if normalize_location(state) == normalize_location(state_of_residence)),
            None,
        )
        if not selected_state:
            return registration_page(
                **form_context,
                error="Please select your state of residence."
            )

        if len(city_of_residence) > 100:
            return registration_page(
                **form_context,
                error="City/town must be 100 characters or fewer."
            )

        if lga_of_residence and lga_of_residence not in NIGERIA_LGAS.get(selected_state, []):
            return registration_page(
                **form_context,
                error="Please select an LGA that belongs to your selected state."
            )

        existing_user = next(
            (item for item in users
             if str(item.get("email", "")).strip().lower() == email),
            None
        )
        if existing_user:
            if existing_user.get("email_verified", True) is False:
                return registration_page(
                    **form_context,
                    error="An account already exists with this email but has not been verified. Please use the resend verification link below.",
                    show_resend=True
                )
            return registration_page(
                **form_context,
                error="An account with that email already exists."
            )

        user = {
            "id": max((int(item.get("id", 0)) for item in users), default=0) + 1,
            "name": name,
            "email": email,
            "password": generate_password_hash(password),
            "saved_places": [],
            "state_of_residence": selected_state,
            "city_of_residence": city_of_residence,
            "lga_of_residence": lga_of_residence,
            # Email verification is temporarily disabled. Keep new accounts login-ready.
            "email_verified": True,
            "created_at": datetime.now().isoformat(timespec="seconds")
        }
        users.append(user)

        try:
            # Save immediately so the account survives a Flask restart.
            save_app_state()
        except Exception:
            # Roll back the in-memory addition if persistence failed, rather than
            # telling the user an account was safely created when it was not.
            users[:] = [item for item in users if str(item.get("id")) != str(user["id"])]
            app.logger.exception("Could not persist newly registered account")
            return registration_page(
                **form_context,
                error="We couldn't save your account right now. Please try again."
            )

        return registration_page(
            message="Your account has been created successfully. You can now log in.",
            email=email,
            verification_sent=False
        )

    return registration_page()


EMAIL_VERIFICATION_HOURS = 24


def send_resend_email(to_email, subject, text_body):
    """Send an email through the Resend HTTP API (not SMTP)."""
    global HEALTH_LAST_EMAIL_ERROR, HEALTH_LAST_EMAIL_SUCCESS_AT
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("RESEND_FROM", "onboarding@resend.dev").strip()

    if not api_key:
        HEALTH_LAST_EMAIL_ERROR = "RESEND_API_KEY is not configured."
        raise RuntimeError(
            "Resend is not configured. Add RESEND_API_KEY to the project's .env file, "
            "then restart Flask."
        )
    if not sender:
        HEALTH_LAST_EMAIL_ERROR = "RESEND_FROM is empty."
        raise RuntimeError("RESEND_FROM is empty. Configure a valid Resend sender address.")

    payload = json.dumps({
        "from": sender,
        "to": [to_email],
        "subject": subject,
        "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "THE-ECOSYSTEM-Flask-App",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Resend API rejected the email (HTTP {response.status}). Details: {response_body}"
                )
            try:
                result = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                result = {}
            HEALTH_LAST_EMAIL_ERROR = ""
            HEALTH_LAST_EMAIL_SUCCESS_AT = datetime.now(timezone.utc).isoformat()
            app.logger.info(
                "Resend accepted email request: recipient=%s, email_id=%s",
                to_email, result.get("id", "unknown")
            )
            return result
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        # Never log the API key or Authorization header.
        HEALTH_LAST_EMAIL_ERROR = f"Resend API returned HTTP {exc.code}."
        app.logger.error("Resend API error HTTP %s: %s", exc.code, details)
        raise RuntimeError(
            f"Resend API rejected the email (HTTP {exc.code}). Details: {details}"
        ) from exc
    except urllib.error.URLError as exc:
        HEALTH_LAST_EMAIL_ERROR = "Could not reach Resend API."
        app.logger.error("Could not reach Resend API: %s", exc.reason)
        raise RuntimeError(
            f"Could not reach Resend API: {exc.reason}. Check internet connectivity."
        ) from exc


def send_email_verification_email(user, verification_link):
    body = (
        f"Hello {user.get('name', 'there')},\n\n"
        "Thanks for joining THE ECOSYSTEM. Please verify your email address using the link below.\n\n"
        f"This link expires in {EMAIL_VERIFICATION_HOURS} hours and can only be used once:\n"
        f"{verification_link}\n\n"
        "If you did not create this account, you can ignore this email.\n\n"
        "THE ECOSYSTEM"
    )
    send_resend_email(
        user["email"],
        "Verify your THE ECOSYSTEM email",
        body,
    )


@app.route("/verify-email/<token>", methods=["GET"])
def verify_email(token):
    token_hash = hash_reset_token(token)
    now_timestamp = datetime.now(timezone.utc).timestamp()

    # Find a valid, unexpired verification token.
    with connect_database() as connection:
        connection.row_factory = sqlite3.Row

        record = connection.execute(
            """
            SELECT token_hash, user_id
            FROM email_verification_tokens
            WHERE token_hash = ? AND expires_at > ?
            """,
            (token_hash, now_timestamp)
        ).fetchone()

    # Invalid, expired, or previously used link.
    if not record:
        return render_template_string(
            """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport"
                      content="width=device-width, initial-scale=1.0">
                <meta name="theme-color" content="#050505">
                <title>Verification Link Unavailable — THE ECOSYSTEM</title>
                """
            + PASSWORD_RECOVERY_STYLE +
            """
            </head>
            <body>
                <main class="recovery-card">
                    <div class="brand">THE ECOSYSTEM</div>

                    <h1>Verification link unavailable</h1>

                    <p>
                        This verification link is invalid, expired, or has
                        already been used. Request a new verification link
                        to continue.
                    </p>

                    <a class="back"
                       href="{{ url_for('resend_verification') }}">
                        Request another verification link
                    </a>
                </main>
            </body>
            </html>
            """
        ), 400

    # Find the account associated with the token.
    user = next(
        (
            item for item in users
            if str(item.get("id")) == str(record["user_id"])
        ),
        None
    )

    if user is None:
        with connect_database() as connection:
            connection.execute(
                """
                DELETE FROM email_verification_tokens
                WHERE token_hash = ?
                """,
                (token_hash,)
            )
            connection.commit()

        flash(
            "We couldn't find the account associated with this link. "
            "Please register again or contact support.",
            "error"
        )
        return redirect(url_for("login"))

    # Mark the account as verified.
    user["email_verified"] = True

    # Invalidate all outstanding verification links for this account.
    with connect_database() as connection:
        connection.execute(
            """
            DELETE FROM email_verification_tokens
            WHERE user_id = ?
            """,
            (str(user["id"]),)
        )
        connection.commit()

    # Persist the verified status before displaying the success page.
    save_app_state()

    # Display a dedicated success page with a login link.
    return render_template("verification_success.html")



@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    error = None
    message = None
    email_value = request.args.get("email", "").strip().lower()

    if request.method == "POST":
        email_value = request.form.get("email", "").strip().lower()
        if not email_value or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email_value):
            error = "Please enter a valid email address."
        else:
            # Keep the response generic when no eligible account exists, but only
            # display a success message after the email provider accepts the send.
            message = "If an unverified account exists for that email, a new verification link will be sent."
            user = next(
                (item for item in users
                 if str(item.get("email", "")).strip().lower() == email_value),
                None
            )
            if user and user.get("email_verified", True) is False:
                try:
                    token = secrets.token_urlsafe(32)
                    token_hash = hash_reset_token(token)
                    expires_at = (
                        datetime.now(timezone.utc) + timedelta(hours=EMAIL_VERIFICATION_HOURS)
                    ).timestamp()
                    with connect_database() as connection:
                        connection.execute(
                            "DELETE FROM email_verification_tokens WHERE expires_at <= ? OR user_id = ?",
                            (datetime.now(timezone.utc).timestamp(), str(user["id"]))
                        )
                        connection.execute(
                            "INSERT INTO email_verification_tokens (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                            (token_hash, str(user["id"]), expires_at)
                        )
                        connection.commit()
                    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
                    if not base_url:
                        base_url = request.url_root.rstrip("/")
                    verification_link = f"{base_url}{url_for('verify_email', token=token)}"
                    send_email_verification_email(user, verification_link)
                    message = "Link successfully sent. Please check your inbox and spam folder."
                except Exception:
                    app.logger.exception("Could not resend email verification message")
                    with connect_database() as connection:
                        connection.execute(
                            "DELETE FROM email_verification_tokens WHERE user_id = ?",
                            (str(user["id"]),)
                        )
                        connection.commit()
                    message = None
                    error = "We couldn't send the verification link right now. Please try again shortly."

    return render_template_string(
        """<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"><meta name=\"theme-color\" content=\"#050505\"><title>Verify Email — THE ECOSYSTEM</title>""" + PASSWORD_RECOVERY_STYLE + """</head><body><main class=\"recovery-card\"><div class=\"brand\">THE ECOSYSTEM</div><h1>Verify your email</h1><p>Enter the email address you used to register. If the account needs verification, we'll send a fresh link.</p>{% if error %}<div class=\"message\">{{ error }}</div>{% endif %}{% if message %}<div class=\"message\">{{ message }}</div>{% endif %}<form method=\"POST\"><label for=\"email\">Email address</label><input id=\"email\" name=\"email\" type=\"email\" value=\"{{ email_value }}\" autocomplete=\"email\" required><button type=\"submit\">Send verification link</button></form><a class=\"back\" href=\"{{ url_for('login') }}\">Back to login</a></main></body></html>""",
        error=error, message=message, email_value=email_value
    )

# =========================
# P12.1 — LOGIN
# =========================


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template(
                "login.html",
                error="Please enter both your email and password.",
                next=request.args.get("next", "")
            )

        # Normalize older account emails too, not just newly registered ones.
        user = next(
            (
                item for item in users
                if str(item.get("email", "")).strip().lower() == email
            ),
            None
        )

        if user is None:
            return render_template(
                "login.html",
                error="Incorrect email or password.",
                next=request.args.get("next", "")
            )

        stored_password = str(user.get("password", ""))
        password_valid = False

        if stored_password.startswith(("scrypt:", "pbkdf2:")):
            try:
                password_valid = check_password_hash(stored_password, password)
            except (ValueError, TypeError):
                app.logger.exception("Stored password hash could not be verified")
                password_valid = False
        else:
            # Migrate legacy plain-text accounts to a hash after successful login.
            password_valid = (stored_password == password)
            if password_valid:
                user["password"] = generate_password_hash(password)

        if not password_valid:
            return render_template(
                "login.html",
                error="Incorrect email or password.",
                next=request.args.get("next", "")
            )

        # Email verification is temporarily bypassed; existing and new accounts
        # can log in while verification is paused.
        if user.get("email_verified") is False:
            user["email_verified"] = True

        session.clear()
        session["user_id"] = user["id"]

        # Persist a legacy-password migration immediately as well as through after_request.
        try:
            save_app_state()
        except Exception:
            app.logger.exception("Could not save user state after login")

        next_url = request.args.get("next") or request.form.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)

        return redirect(url_for("account"))

    return render_template(
        "login.html",
        next=request.args.get("next", "")
    )

# =========================
# PASSWORD RECOVERY — EMAIL RESET LINK
# =========================

PASSWORD_RESET_MINUTES = 30

PASSWORD_RECOVERY_STYLE = """
<style>
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; padding: 24px; display: grid;
        place-items: center; background: #050505; color: #f5f5f5;
        font-family: Arial, sans-serif; }
    .recovery-card { width: 100%; max-width: 440px; padding: 32px;
        border: 1px solid #292020; border-radius: 18px;
        background: linear-gradient(145deg, #151010, #080808);
        box-shadow: 0 0 45px rgba(220, 20, 60, .12); }
    .brand { color: #ff334f; letter-spacing: 3px; font-size: 12px;
        font-weight: 700; margin-bottom: 24px; }
    h1 { font-size: 27px; margin: 0 0 12px; }
    p { color: #b8b8b8; line-height: 1.6; font-size: 14px; }
    label { display: block; margin: 18px 0 8px; font-size: 13px; color: #ddd; }
    input { width: 100%; padding: 13px 14px; border-radius: 9px;
        border: 1px solid #383030; background: #090909; color: white; }
    button { width: 100%; padding: 14px; margin-top: 20px; border: 0;
        border-radius: 9px; background: linear-gradient(90deg, #c8102e, #ff334f);
        color: white; font-weight: 700; cursor: pointer; }
    a { color: #ff5269; text-decoration: none; }
    .message { padding: 11px; margin: 14px 0; border-radius: 8px;
        background: #211216; color: #ffdce2; font-size: 13px; }
    .back { display: block; margin-top: 22px; text-align: center; font-size: 13px; }
</style>
"""


def hash_reset_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_password_reset_email(user, reset_link):
    body = (
        f"Hello {user.get('name', 'there')},\n\n"
        "We received a request to reset your THE ECOSYSTEM password.\n\n"
        f"Use this link within {PASSWORD_RESET_MINUTES} minutes to choose a new password:\n"
        f"{reset_link}\n\n"
        "If you did not request this, you can ignore this email. Your password will not change.\n\n"
        "THE ECOSYSTEM"
    )
    send_resend_email(
        user["email"],
        "Reset your THE ECOSYSTEM password",
        body,
    )


def get_valid_reset_record(token):
    if not token:
        return None
    token_hash = hash_reset_token(token)
    now_timestamp = datetime.now(timezone.utc).timestamp()
    with connect_database() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT token_hash, user_id, expires_at FROM password_reset_tokens "
            "WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now_timestamp)
        ).fetchone()
    return dict(row) if row else None


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Please enter a valid email address."
        else:
            # Always show the same response so this page does not reveal which
            # email addresses have registered accounts.
            message = (
                "If an account exists for that email and email delivery is configured, "
                "a password reset link will be sent shortly."
            )
            user = next(
                (item for item in users
                 if str(item.get("email", "")).strip().lower() == email),
                None
            )
            if user:
                try:
                    token = secrets.token_urlsafe(32)
                    token_hash = hash_reset_token(token)
                    expires_at = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=PASSWORD_RESET_MINUTES)
                    ).timestamp()
                    with connect_database() as connection:
                        connection.execute(
                            "DELETE FROM password_reset_tokens WHERE expires_at <= ? OR user_id = ?",
                            (datetime.now(timezone.utc).timestamp(), str(user["id"]))
                        )
                        connection.execute(
                            "INSERT INTO password_reset_tokens (token_hash, user_id, expires_at) "
                            "VALUES (?, ?, ?)",
                            (token_hash, str(user["id"]), expires_at)
                        )
                        connection.commit()
                    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
                    if not base_url:
                        base_url = request.url_root.rstrip("/")
                    reset_link = f"{base_url}{url_for('reset_password', token=token)}"
                    send_password_reset_email(user, reset_link)
                except Exception:
                    app.logger.exception("Could not send password reset email")
                    # Do not expose SMTP errors or account existence to visitors.
                    with connect_database() as connection:
                        connection.execute(
                            "DELETE FROM password_reset_tokens WHERE user_id = ?",
                            (str(user["id"]),)
                        )
                        connection.commit()

    return render_template_string(
        """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="theme-color" content="#050505"><title>Forgot Password — THE ECOSYSTEM</title>
        """ + PASSWORD_RECOVERY_STYLE + """
        </head><body><main class="recovery-card"><div class="brand">THE ECOSYSTEM</div>
        <h1>Forgot your password?</h1><p>Enter the email address linked to your account. If it exists, we'll send you a secure reset link.</p>
        {% if error %}<div class="message">{{ error }}</div>{% endif %}
        {% if message %}<div class="message">{{ message }}</div>{% endif %}
        <form method="POST"><label for="email">Email address</label>
        <input id="email" name="email" type="email" autocomplete="email" required>
        <button type="submit">Send reset link</button></form>
        <a class="back" href="{{ url_for('login') }}">Back to login</a></main></body></html>""",
        error=error, message=message
    )


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset_record = get_valid_reset_record(token)
    if not reset_record:
        return render_template_string(
            """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Reset Link Expired — THE ECOSYSTEM</title>""" + PASSWORD_RECOVERY_STYLE + """
            </head><body><main class="recovery-card"><div class="brand">THE ECOSYSTEM</div>
            <h1>Reset link unavailable</h1><p>This password reset link is invalid, expired, or has already been used.</p>
            <a class="back" href="{{ url_for('forgot_password') }}">Request another reset link</a></main></body></html>"""
        ), 400

    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(password) < 8:
            error = "Your password must contain at least 8 characters."
        elif password != confirm_password:
            error = "The passwords do not match."
        else:
            user = next(
                (item for item in users if str(item.get("id")) == str(reset_record["user_id"])),
                None
            )
            if user is None:
                with connect_database() as connection:
                    connection.execute(
                        "DELETE FROM password_reset_tokens WHERE token_hash = ?",
                        (reset_record["token_hash"],)
                    )
                    connection.commit()
                return redirect(url_for("forgot_password"))

            # Consume every outstanding reset token for this account so links
            # cannot be reused and older links are invalidated.
            with connect_database() as connection:
                connection.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?",
                    (str(user["id"]),)
                )
                connection.commit()
            user["password"] = generate_password_hash(password)
            try:
                save_app_state()
            except Exception:
                app.logger.exception("Could not persist password reset")
                error = "We couldn't save your new password. Please try again."
            if error is None:
                flash("Your password has been reset. Please log in.", "success")
                return redirect(url_for("login"))

    return render_template_string(
        """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta name="theme-color" content="#050505"><title>Choose a New Password — THE ECOSYSTEM</title>
        """ + PASSWORD_RECOVERY_STYLE + """
        </head><body><main class="recovery-card"><div class="brand">THE ECOSYSTEM</div>
        <h1>Choose a new password</h1><p>Use at least 8 characters. This reset link can only be used once.</p>
        {% if error %}<div class="message">{{ error }}</div>{% endif %}
        <form method="POST"><label for="password">New password</label>
        <input id="password" name="password" type="password" autocomplete="new-password" minlength="8" required>
        <label for="confirm_password">Confirm new password</label>
        <input id="confirm_password" name="confirm_password" type="password" autocomplete="new-password" minlength="8" required>
        <button type="submit">Reset password</button></form></main></body></html>""",
        error=error
    )


# =========================
# P12.1 — ACCOUNT
# =========================

@app.route("/account/residence", methods=["POST"])
def update_residence():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login", next="/account"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    state_of_residence = request.form.get("state_of_residence", "").strip()
    city_of_residence = request.form.get("city_of_residence", "").strip()
    lga_of_residence = request.form.get("lga_of_residence", "").strip()

    selected_state = next(
        (state for state in NIGERIA_STATES
         if normalize_location(state) == normalize_location(state_of_residence)),
        None,
    )
    if not selected_state:
        flash("Please select a valid state of residence.", "error")
        return redirect(url_for("account"))

    if len(city_of_residence) > 100:
        flash("City/town must be 100 characters or fewer.", "error")
        return redirect(url_for("account"))

    if lga_of_residence and lga_of_residence not in NIGERIA_LGAS.get(selected_state, []):
        flash("Please select an LGA from your chosen state, or leave it blank.", "error")
        return redirect(url_for("account"))

    user["state_of_residence"] = selected_state
    user["city_of_residence"] = city_of_residence
    user["lga_of_residence"] = lga_of_residence
    save_app_state()
    flash("Your residence and default discovery state have been updated.", "success")
    return redirect(url_for("account"))


@app.route("/account")
def account():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    submitted_businesses = []
    for owned_item in get_user_owned_businesses(user_id):
        business = owned_item["business"]
        business_status = str(owned_item.get("status", business.get("status", "APPROVED"))).strip().upper()
        # The card's Active/Inactive indicator follows the actual listing approval
        # and moderation state, not whether an ownership claim happens to exist.
        is_active = (
            business_status == "APPROVED"
            and not business.get("archived")
            and not business.get("ownership_dispute")
            and str(business.get("status", "APPROVED")).strip().upper() not in {
                "PENDING", "PENDING REVIEW", "PENDING_REVIEW", "REJECTED",
                "HIDDEN", "DISPUTED", "DELETED", "ARCHIVED"
            }
        )
        submitted_businesses.append({
            "business": business,
            "claim": owned_item["claim"],
            "status": owned_item["status"],
            "role": owned_item["role"],
            "is_active": is_active,
            # Use the same helper as the public business card/profile so each
            # account card reads the latest saved business profile image.
            "card_image_url": get_business_card_image(business),
            "engagement": get_business_engagement_metrics(business.get("id")),
            "review_count": len(get_business_reviews(business.get("id"))),
            "review_replied": sum(1 for review in get_business_reviews(business.get("id")) if review.get("reply") or review.get("business_reply") or review.get("owner_reply")),
            "review_unanswered": sum(1 for review in get_business_reviews(business.get("id")) if not (review.get("reply") or review.get("business_reply") or review.get("owner_reply"))),
        })
    user_notifications = sorted([n for n in business_notifications if str(n.get("user_id")) == str(user_id)], key=lambda n: n.get("created_at", ""), reverse=True)[:30]

    submitted_events = sorted(
        [event for event in event_submissions if str(event.get("user_id")) == str(user_id)],
        key=lambda item: item.get("id", 0),
        reverse=True,
    )
    refresh_expired_event_statuses()

    return render_template(
        "account.html",
        user=user,
        submitted_businesses=submitted_businesses,
        submitted_events=submitted_events,
        business_notifications=user_notifications,
        unread_business_notifications=sum(1 for n in user_notifications if not n.get("read")),
        premium_orders=sorted(
            [order for order in premium_listing_orders
             if str(order.get("user_id")) == str(user_id)],
            key=lambda order: order.get("created_at", ""),
            reverse=True,
        ),
        states=NIGERIA_STATES,
        home_state=get_user_home_state(user),
    )

# =========================
# P12.2 — EDIT PROFILE
# =========================

@app.route("/account/edit", methods=["GET", "POST"])
def edit_profile():
    user_id = session.get("user_id")

    if user_id is None:
        return redirect(url_for("login", next="/account/edit"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    def render_member_profile(error=None):
        # Start with businesses recognised by the normal ownership helper.
        owned_by_id = {}
        for owned in get_user_owned_businesses(user_id):
            business = owned.get("business") or {}
            business_id = business.get("id")
            if business_id is not None:
                owned_by_id[str(business_id)] = owned

        # Also include listings submitted by this member. New submissions can
        # still be pending moderation, and must appear on the member profile.
        for submission in business_submissions:
            if str(submission.get("user_id")) != str(user_id):
                continue
            business_id = submission.get("business_id")
            business = businesses.get(business_id)
            if business is None:
                try:
                    business = businesses.get(int(business_id))
                except (TypeError, ValueError):
                    business = None
            if not business:
                continue
            key = str(business.get("id", business_id))
            if key not in owned_by_id:
                submission_status = str(submission.get("status", business.get("status", "PENDING"))).upper()
                rating = get_business_rating(business.get("id"))
                owned_by_id[key] = {
                    "business": business,
                    "claim": get_business_owner_claim(business.get("id"), user_id),
                    "submission": submission,
                    "status": submission_status,
                    "rating": rating,
                    "is_verified": is_business_verified(business.get("id")),
                    "review_count": rating.get("count", 0),
                    "photo_count": len(business.get("photos") or []),
                    "role": "PRIMARY OWNER",
                }

        owned_businesses = sorted(
            owned_by_id.values(),
            key=lambda item: (
                str(item.get("status", "")).upper() not in {"PENDING", "PENDING REVIEW"},
                str((item.get("business") or {}).get("name", "")).lower(),
            ),
        )

        showcase_posts_for_user = [
            post for post in showcase_posts
            if str(post.get("user_id", "")) == str(user_id)
        ]
        showcase_posts_for_user.sort(
            key=lambda post: str(post.get("created_at", "")), reverse=True
        )

        user_reviews = [
            review for review in reviews
            if str(review.get("user_id", "")) == str(user_id)
        ]
        user_reviews.sort(
            key=lambda review: str(review.get("created_at", "")), reverse=True
        )

        award_winners = get_published_award_winners(user_id=user_id)

        return render_template(
            "edit_profile.html",
            user=user,
            error=error,
            owned_businesses=owned_businesses,
            showcase_posts=showcase_posts_for_user,
            user_reviews=user_reviews,
            award_winners=award_winners,
        )

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not name or not email:
            return render_member_profile("Name and email are required.")

        if len(name) > 100:
            return render_member_profile("Your name must be 100 characters or fewer.")

        if not re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email):
            return render_member_profile("Please enter a valid email address.")

        email_in_use = any(
            str(other_user.get("email", "")).lower() == email
            and str(other_user.get("id")) != str(user_id)
            for other_user in users
        )
        if email_in_use:
            return render_member_profile("That email address is already in use.")

        user["name"] = name
        user["email"] = email

        for review in reviews:
            if str(review.get("user_id", "")) == str(user_id):
                review["user_name"] = name

        save_app_state()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("edit_profile"))

    return render_member_profile()


# =========================
# P12.3 — CHANGE PASSWORD
# =========================

@app.route("/account/change-password", methods=["GET", "POST"])
def change_password():
    user_id = session.get("user_id")

    if user_id is None:
        return redirect(
            url_for("login", next="/account/change-password")
        )

    user = get_user_by_id(user_id)

    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        stored_password = user.get("password", "")

        if stored_password.startswith(("scrypt:", "pbkdf2:")):
            current_password_valid = check_password_hash(
                stored_password,
                current_password
            )
        else:
            # Compatibility with accounts created before hashing.
            current_password_valid = (
                stored_password == current_password
            )

        if not current_password_valid:
            return render_template(
                "change_password.html",
                error="Your current password is incorrect."
            )

        if len(new_password) < 8:
            return render_template(
                "change_password.html",
                error="Your new password must contain at least 8 characters."
            )

        if new_password != confirm_password:
            return render_template(
                "change_password.html",
                error="The new passwords do not match."
            )

        if new_password == current_password:
            return render_template(
                "change_password.html",
                error="Choose a password different from your current one."
            )

        user["password"] = generate_password_hash(new_password)

        # Rotate the session so the previous session cookie is no
        # longer valid for this account.
        session.clear()
        session["user_id"] = user_id

        flash("Your password has been changed successfully.", "success")
        return redirect(url_for("account"))

    return render_template("change_password.html")

# =========================
# BUSINESS OWNER DASHBOARD
# =========================

@app.route("/business-dashboard")
def business_dashboard():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    owned_businesses = get_user_owned_businesses(user_id)
    approved_businesses = [item for item in owned_businesses if item["status"] == "APPROVED"]
    pending_businesses = [item for item in owned_businesses if item["status"] in ("PENDING", "PENDING REVIEW")]
    rejected_businesses = [item for item in owned_businesses if item["status"] == "REJECTED"]
    archived_businesses = [item for item in owned_businesses if item["status"] in ("ARCHIVED", "DISPUTED", "HIDDEN")]
    user_notifications = sorted([n for n in business_notifications if str(n.get("user_id")) == str(user_id)], key=lambda n: n.get("created_at", ""), reverse=True)[:20]

    return render_template(
        "business_dashboard.html",
        user=user,
        owned_businesses=owned_businesses,
        approved_businesses=approved_businesses,
        pending_businesses=pending_businesses,
        rejected_businesses=rejected_businesses,
        archived_businesses=archived_businesses,
        business_notifications=user_notifications,
        total_businesses=len(owned_businesses),
        total_reviews=sum(item["review_count"] for item in owned_businesses),
        total_photos=sum(item["photo_count"] for item in owned_businesses),
        verified_count=sum(1 for item in owned_businesses if item["is_verified"]),
        premium_orders=sorted([order for order in premium_listing_orders if str(order.get("user_id")) == str(user_id)], key=lambda order: order.get("created_at", ""), reverse=True)
    )


# =========================
# P12.6 — MANAGE BUSINESS
# =========================

@app.route("/manage/business/<int:business_id>", methods=["GET", "POST"])
def manage_business(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None:
        return "Business not found", 404

    if not business_has_access(business, user_id):
        flash("You do not have approved management access to this business.", "error")
        return redirect(url_for("account"))
    if business_management_blocked(business):
        flash("Ownership changes and business edits are paused while this ownership dispute is reviewed.", "error")
        return redirect(url_for("account"))
    claim = get_business_owner_claim(business_id, user_id) or {"status": "APPROVED", "role": "Primary owner"}

    if request.method == "POST":
        fields = {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "location": request.form.get("location", "").strip(),
            "description": request.form.get("description", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "whatsapp": request.form.get("whatsapp", "").strip(),
            "instagram": request.form.get("instagram", "").strip(),
            "website": request.form.get("website", "").strip(),
            "opening_hours": request.form.get("opening_hours", "").strip()
        }

        if not all(fields[key] for key in ("name", "category", "location", "description")):
            return render_template(
                "manage_business.html",
                business=business,
                user=user,
                claim=claim,
                profile_picture_url=get_business_profile_picture_url(business),
                error="Please complete all required business information."
            )

        sensitive = [key for key in ("name", "phone", "whatsapp", "website") if str(fields.get(key, "")) != str(business.get(key, ""))]
        if sensitive:
            business_edit_requests = business.setdefault("edit_requests", [])
            business_edit_requests.append({"id": max((int(x.get("id", 0)) for x in business_edit_requests), default=0)+1,
                "user_id": user_id, "fields": fields, "sensitive_fields": sensitive, "status": "PENDING", "created_at": _business_now()})
            record_business_audit("SENSITIVE_EDIT_REQUESTED", business_id, user_id, ", ".join(sensitive))
            notify_business_user(user_id, "Business edit awaiting review", f"Your changes to {business.get('name')} include sensitive fields ({', '.join(sensitive)}) and are awaiting admin review.", business_id)
            save_app_state()
            flash("Sensitive changes were sent for admin review. Routine changes were not applied yet; resubmit routine edits after review.", "success")
        else:
            for key in ("category", "location", "description", "instagram", "opening_hours"):
                business[key] = fields[key]
            business["about"] = fields["description"]
            record_business_audit("BUSINESS_DETAILS_EDITED", business_id, user_id, "Routine business information updated")
            save_app_state()
            flash("Routine business information updated successfully.", "success")
        return redirect(url_for("manage_business", business_id=business_id))

    return render_template(
        "manage_business.html",
        business=business,
        user=user,
        claim=claim,
        profile_picture_url=get_business_profile_picture_url(business),
    )


# =========================
# P12.6 — UPLOAD BUSINESS PHOTOS
# =========================

@app.route("/manage/business/<int:business_id>/photos", methods=["POST"])
def upload_business_photos(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None:
        return "Business not found", 404

    if business.get("submitted_by") != user_id:
        return redirect(url_for("account"))

    claim = get_business_owner_claim(business_id, user_id)
    if claim is None or claim.get("status") != "APPROVED":
        return redirect(url_for("account"))

    files = request.files.getlist("photos")
    if not files:
        flash("Please select at least one photo to upload.", "error")
        return redirect(url_for("manage_business", business_id=business_id))

    business.setdefault("photos", [])
    uploaded_count = 0
    errors = []

    for file in files:
        if not file or not file.filename:
            continue

        if not allowed_file(file.filename):
            errors.append(f"{file.filename}: unsupported file type.")
            continue

        file.stream.seek(0, os.SEEK_END)
        file_size = file.stream.tell()
        file.stream.seek(0)

        if file_size > MAX_PHOTO_SIZE:
            errors.append(f"{file.filename}: larger than 5 MB.")
            continue

        original_filename = secure_filename(file.filename)
        if not original_filename or "." not in original_filename:
            errors.append("One file could not be processed.")
            continue

        extension = original_filename.rsplit(".", 1)[1].lower()
        unique_filename = f"{uuid.uuid4().hex}.{extension}"
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)

        try:
            file.save(file_path)
        except Exception:
            errors.append(f"{file.filename}: upload failed.")
            continue

        photo_url = url_for("static", filename=f"uploads/businesses/{unique_filename}")
        business["photos"].append(photo_url)
        uploaded_count += 1

    if uploaded_count:
        flash(f"{uploaded_count} photo(s) uploaded successfully.", "success")
    if errors:
        flash(" ".join(errors), "error")

    return redirect(url_for("manage_business", business_id=business_id))


# =========================
# P12.6 — DELETE BUSINESS PHOTO
# =========================

@app.route(
    "/manage/business/<int:business_id>/photos/delete/<int:photo_index>",
    methods=["POST"]
)
def delete_business_photo(business_id, photo_index):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    business = businesses.get(business_id)
    if business is None:
        return "Business not found", 404

    if business.get("submitted_by") != user_id:
        return redirect(url_for("account"))

    claim = get_business_owner_claim(business_id, user_id)
    if claim is None or claim.get("status") != "APPROVED":
        return redirect(url_for("account"))

    photos = business.get("photos", [])
    if photo_index < 0 or photo_index >= len(photos):
        return redirect(url_for("manage_business", business_id=business_id))

    photo_url = photos[photo_index]
    filename = os.path.basename(photo_url)
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    if os.path.exists(file_path):
        os.remove(file_path)

    photos.pop(photo_index)
    flash("Photo deleted successfully.", "success")
    return redirect(url_for("manage_business", business_id=business_id))


# =========================
# P12.3 — SAVED PLACES
# =========================

@app.route("/saved")
def saved_places():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    saved_businesses = []

    for saved in user.get("saved_places", []):
        if saved["type"] != "business":
            continue

        business = businesses.get(saved["id"])
        if business:
            saved_businesses.append({
                "id": business["id"],
                "name": business["name"],
                "category": business["category"],
                "location": business["location"]
            })

    return render_template("saved.html", user=user, saved_businesses=saved_businesses)


@app.route("/save/business/<int:business_id>")
def save_business(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    if business_id not in businesses:
        return "Business not found", 404

    saved_places = user.setdefault("saved_places", [])
    already_saved = any(
        saved["type"] == "business" and saved["id"] == business_id
        for saved in saved_places
    )

    if not already_saved:
        saved_places.append({"type": "business", "id": business_id})

    return redirect(url_for("business_profile", business_id=business_id))


@app.route("/remove/business/<int:business_id>")
def remove_saved_business(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    user["saved_places"] = [
        saved for saved in user.get("saved_places", [])
        if not (saved["type"] == "business" and saved["id"] == business_id)
    ]

    return redirect(url_for("saved_places"))


# =========================
# P12.4 — CLAIM BUSINESS
# =========================

@app.route("/claim/business/<int:business_id>", methods=["GET", "POST"])
def claim_business(business_id):
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))
    user = get_user_by_id(user_id)
    business = businesses.get(business_id)
    if not user or not business:
        return ("Business not found", 404) if not business else redirect(url_for("login"))
    if business_has_access(business, user_id):
        flash("You already have access to this business.", "info")
        return redirect(url_for("business_dashboard"))
    existing_claim = get_business_owner_claim(business_id, user_id)
    if request.method == "POST":
        role = request.form.get("role", "Owner").strip()
        message = request.form.get("message", "").strip()
        evidence_contact = request.form.get("evidence_contact", "").strip()
        evidence_details = request.form.get("evidence_details", "").strip()
        if existing_claim and existing_claim.get("status") == "PENDING":
            flash("Your claim is already awaiting review.", "info")
            return redirect(url_for("account"))
        if role not in {"Owner", "Manager", "Authorized representative"} or not message or not evidence_contact or not evidence_details:
            return render_template_string("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Claim business</title><style>body{background:#080909;color:#fff;font:15px Arial;max-width:680px;margin:50px auto;padding:20px}input,textarea,select,button{display:block;width:100%;box-sizing:border-box;margin:10px 0 18px;padding:13px;background:#111413;color:#fff;border:1px solid #333;border-radius:8px}button{background:#80C995;color:#080909;font-weight:800;border:0}a{color:#80C995}</style></head><body><h1>Claim {{ business.name }}</h1><p>Provide business contact evidence so an admin can verify your connection.</p><form method="post"><label>Claim role</label><select name="role"><option>Owner</option><option>Manager</option><option>Authorized representative</option></select><label>Business phone or email used for verification</label><input name="evidence_contact" required><label>Evidence details / public business link</label><input name="evidence_details" required><label>Explain your connection</label><textarea name="message" required></textarea><button>Submit claim for review</button></form><a href="{{ url_for('account') }}">Back to account</a></body></html>""", business=business)
        claim = {"id": max((int(x.get("id", 0)) for x in business_claims), default=0)+1, "user_id": user_id,
            "business_id": business_id, "business_name": business.get("name", ""), "role": role,
            "message": message, "evidence_contact": evidence_contact, "evidence_details": evidence_details,
            "status": "PENDING", "created_at": _business_now(), "claim_type": "OWNERSHIP"}
        business_claims.append(claim)
        notify_admin("Business ownership claim submitted", f"{user.get('name', 'A user')} submitted a {role.lower()} claim for {business.get('name')}.", "claim", claim.get("id"))
        record_business_audit("OWNERSHIP_CLAIM_SUBMITTED", business_id, user_id, f"Role: {role}; evidence supplied")
        save_app_state()
        flash("Ownership claim submitted. Admin verification is required before access is granted.", "success")
        return redirect(url_for("account"))
    return render_template_string("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Claim business</title><style>body{background:#080909;color:#fff;font:15px Arial;max-width:680px;margin:50px auto;padding:20px}input,textarea,select,button{display:block;width:100%;box-sizing:border-box;margin:10px 0 18px;padding:13px;background:#111413;color:#fff;border:1px solid #333;border-radius:8px}button{background:#80C995;color:#080909;font-weight:800;border:0}a{color:#80C995}</style></head><body><h1>Claim {{ business.name }}</h1><p>Provide business contact evidence so an admin can verify your connection.</p><form method="post"><label>Claim role</label><select name="role"><option>Owner</option><option>Manager</option><option>Authorized representative</option></select><label>Business phone or email used for verification</label><input name="evidence_contact" required><label>Evidence details / public business link</label><input name="evidence_details" required><label>Explain your connection</label><textarea name="message" required></textarea><button>Submit claim for review</button></form><a href="{{ url_for('account') }}">Back to account</a></body></html>""", business=business)


# =========================
# PREMIUM BUSINESS LISTINGS
# =========================

PREMIUM_LISTING_RATE_NAIRA = 2999
PREMIUM_LISTING_RATE_KOBO = PREMIUM_LISTING_RATE_NAIRA * 100
# VAT is itemized separately and included in the amount verified by Paystack.
PREMIUM_LISTING_VAT_RATE = 0.075


def _premium_listing_vat(subtotal_naira):
    """Calculate 7.5% VAT, rounded to the nearest whole naira (half-up)."""
    subtotal = max(0, int(subtotal_naira))
    return (subtotal * 75 + 500) // 1000


PREMIUM_LISTING_DURATIONS = {1: "1 month", 3: "3 months", 6: "6 months", 12: "12 months"}


def _premium_listing_user():
    uid = session.get("user_id")
    return get_user_by_id(uid) if uid is not None else None


def _premium_listing_order(order_id, user_id=None):
    return next((item for item in premium_listing_orders
                 if str(item.get("id")) == str(order_id)
                 and (user_id is None or str(item.get("user_id")) == str(user_id))), None)


def _premium_upload(file_storage, allowed_extensions, max_bytes, label):
    if not file_storage or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if "." not in filename or filename.rsplit(".", 1)[1].lower() not in allowed_extensions:
        raise ValueError(f"{label} has an unsupported file type.")
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_bytes:
        raise ValueError(f"{label} is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")
    unique_name = f"premium_{uuid.uuid4().hex}_{filename}"
    file_storage.save(os.path.join(app.config["PREMIUM_MEDIA_FOLDER"], unique_name))
    return "uploads/premium_listings/" + unique_name


@app.route("/premium-listing")
def premium_listing_plans():
    user = _premium_listing_user()
    return render_template("premium_listing_plans.html", user=user,
                           rate=PREMIUM_LISTING_RATE_NAIRA,
                           vat_rate=PREMIUM_LISTING_VAT_RATE,
                           durations=PREMIUM_LISTING_DURATIONS)


@app.route("/premium-listing/campaign", methods=["GET", "POST"])
def premium_listing_campaign_start():
    """New multi-item campaign entry point; leaves the legacy application flow intact."""
    user = _premium_listing_user()
    if not user:
        flash("Please sign in before starting a Premium Listing campaign.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_campaign_start")))

    saved = session.get("premium_campaign_selection", {})
    error = ""
    if request.method == "POST":
        try:
            business_count = int(request.form.get("business_count", "0"))
            event_count = int(request.form.get("event_count", "0"))
        except (TypeError, ValueError):
            business_count, event_count = -1, -1

        if not (0 <= business_count <= 20 and 0 <= event_count <= 20):
            error = "Choose a valid quantity between 0 and 20 for each type."
        elif business_count + event_count < 1:
            error = "Choose at least one business or event to continue."
        else:
            session["premium_campaign_selection"] = {
                "business_count": business_count,
                "event_count": event_count,
            }
            session.pop("premium_campaign_selected_business_ids", None)
            session.pop("premium_campaign_selected_event_ids", None)
            session.modified = True
            return redirect(url_for("premium_listing_campaign_listings"))

    return render_template(
        "premium_listing_campaign.html",
        user=user,
        selection=saved,
        error=error,
    )


@app.route("/premium-listing/campaign/listings", methods=["GET", "POST"])
def premium_listing_campaign_listings():
    """Let a signed-in user select the exact existing businesses and submitted events for a campaign."""
    user = _premium_listing_user()
    if not user:
        flash("Please sign in before selecting campaign listings.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_campaign_listings")))

    requested = session.get("premium_campaign_selection", {})
    try:
        business_count = int(requested.get("business_count", 0))
        event_count = int(requested.get("event_count", 0))
    except (TypeError, ValueError):
        business_count, event_count = 0, 0
    if business_count + event_count < 1:
        flash("Choose how many businesses or events to promote first.", "error")
        return redirect(url_for("premium_listing_campaign_start"))

    owned_businesses = []
    for owned_item in get_user_owned_businesses(user.get("id")):
        business = owned_item.get("business", {})
        # Premium campaigns can only be attached to existing, approved public listings.
        if not is_public_business(business):
            continue
        if business.get("archived") or str(business.get("status", "APPROVED")).upper() in {"DELETED", "ARCHIVED", "REJECTED", "HIDDEN", "DISPUTED"}:
            continue
        owned_businesses.append({
            "id": str(business.get("id")),
            "name": business.get("name", "Business"),
            "category": business.get("category", ""),
            "state": business.get("state") or business.get("state_of_operation") or business.get("location", ""),
            "location": business.get("location", ""),
        })

    # Events are limited to records actually submitted by the signed-in user.
    user_events = []
    for event in event_submissions:
        if str(event.get("user_id")) != str(user.get("id")):
            continue
        if event.get("deleted") or str(event.get("status", "")).upper() in {"DELETED", "REJECTED", "HIDDEN", "CANCELLED", "EXPIRED"}:
            continue
        user_events.append({
            "id": str(event.get("id")),
            "title": event.get("title", "Untitled event"),
            "category": event.get("category", ""),
            "state": event.get("state", ""),
            "location": event.get("location") or event.get("venue") or event.get("city", ""),
            "date": event.get("event_date") or event.get("date", ""),
            "status": event.get("status", "PENDING"),
        })
    owned_businesses.sort(key=lambda item: item["name"].lower())
    user_events.sort(key=lambda item: item["title"].lower())

    error = ""
    selected_business_ids = [str(x) for x in session.get("premium_campaign_selected_business_ids", [])]
    selected_event_ids = [str(x) for x in session.get("premium_campaign_selected_event_ids", [])]
    if request.method == "POST":
        selected_business_ids = list(dict.fromkeys(str(x) for x in request.form.getlist("business_ids")))
        selected_event_ids = list(dict.fromkeys(str(x) for x in request.form.getlist("event_ids")))
        valid_business_ids = {item["id"] for item in owned_businesses}
        valid_event_ids = {item["id"] for item in user_events}
        if any(x not in valid_business_ids for x in selected_business_ids) or any(x not in valid_event_ids for x in selected_event_ids):
            error = "One or more selected listings are no longer available. Refresh and select again."
        elif len(selected_business_ids) != business_count or len(selected_event_ids) != event_count:
            error = f"Select exactly {business_count} business(es) and {event_count} event(s) to match your previous step."
        else:
            session["premium_campaign_selected_business_ids"] = selected_business_ids
            session["premium_campaign_selected_event_ids"] = selected_event_ids
            session.modified = True
            return redirect(url_for("premium_listing_campaign_states"))

    return render_template(
        "premium_listing_campaign_listings.html",
        user=user,
        business_count=business_count,
        event_count=event_count,
        businesses=owned_businesses,
        events=user_events,
        selected_business_ids=selected_business_ids,
        selected_event_ids=selected_event_ids,
        error=error,
    )


@app.route("/premium-listing/campaign/states", methods=["GET", "POST"])
def premium_listing_campaign_states():
    """Configure primary-state-free coverage for every selected business and event."""
    user = _premium_listing_user()
    if not user:
        flash("Please sign in before configuring campaign coverage.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_campaign_states")))

    selected_business_ids = [str(x) for x in session.get("premium_campaign_selected_business_ids", [])]
    selected_event_ids = [str(x) for x in session.get("premium_campaign_selected_event_ids", [])]
    if not selected_business_ids and not selected_event_ids:
        flash("Choose your campaign listings first.", "error")
        return redirect(url_for("premium_listing_campaign_start"))

    listings = []
    owned_ids = {str(item.get("business", {}).get("id")) for item in get_user_owned_businesses(user.get("id"))}
    valid_businesses = {str(b.get("id")): b for b in businesses.values() if is_public_business(b)}
    for business_id in selected_business_ids:
        business = valid_businesses.get(business_id)
        if not business or business_id not in owned_ids:
            flash("A selected business is no longer available to your account. Please select your listings again.", "error")
            return redirect(url_for("premium_listing_campaign_listings"))
        raw_state = get_business_state(business)
        primary_state = next((state for state in NIGERIA_STATES if normalize_location(state) == normalize_location(raw_state)), "")
        if not primary_state:
            flash(f"We could not identify the primary state for {business.get('name', 'a selected business')}. Update its location before continuing.", "error")
            return redirect(url_for("premium_listing_campaign_listings"))
        listings.append({"key": "business:" + business_id, "kind": "business", "id": business_id,
                         "name": business.get("name", "Business"), "location": business.get("location", ""),
                         "primary_state": primary_state})

    valid_events = {str(e.get("id")): e for e in event_submissions
                    if str(e.get("user_id")) == str(user.get("id"))
                    and not e.get("deleted")
                    and str(e.get("status", "")).upper() not in {"DELETED", "REJECTED", "HIDDEN", "CANCELLED", "EXPIRED"}}
    for event_id in selected_event_ids:
        event = valid_events.get(event_id)
        if not event:
            flash("A selected event is no longer available. Please select your listings again.", "error")
            return redirect(url_for("premium_listing_campaign_listings"))
        raw_state = event.get("state") or event.get("state_of_event") or event.get("state_of_operation") or ""
        normalized_event_state = normalize_location(raw_state)
        if normalized_event_state in {"abuja", "fct", "fct abuja", "federal capital", "federal capital territory"}:
            normalized_event_state = normalize_location("Federal Capital Territory")
        primary_state = next((state for state in NIGERIA_STATES if normalize_location(state) == normalized_event_state), "")
        if not primary_state:
            flash(f"We could not identify the primary state for {event.get('title', 'a selected event')}. Update its event state before continuing.", "error")
            return redirect(url_for("premium_listing_campaign_listings"))
        listings.append({"key": "event:" + event_id, "kind": "event", "id": event_id,
                         "name": event.get("title", "Event"), "location": event.get("location") or event.get("venue") or event.get("city", ""),
                         "primary_state": primary_state})

    saved = session.get("premium_campaign_state_selections", {})
    error = ""
    duration = 1
    if request.method == "POST":
        try:
            duration = int(request.form.get("duration_months", "1"))
        except (TypeError, ValueError):
            duration = 0
        if duration not in PREMIUM_LISTING_DURATIONS:
            error = "Choose a valid campaign duration."
        else:
            new_selections = {}
            for listing in listings:
                field_name = "states__" + listing["key"].replace(":", "__")
                requested_states = list(dict.fromkeys(str(x) for x in request.form.getlist(field_name)))
                valid_states = set(NIGERIA_STATES)
                if any(state not in valid_states for state in requested_states):
                    error = "One or more selected states are invalid. Please select states from the map."
                    break
                if listing["primary_state"] in requested_states:
                    error = f"{listing['primary_state']} is already included free for {listing['name']}; choose additional states only."
                    break
                if len(requested_states) > len(NIGERIA_STATES) - 1:
                    error = f"Choose no more than {len(NIGERIA_STATES) - 1} additional states for {listing['name']}."
                    break
                target_raw = request.form.get("target_count__" + listing["key"].replace(":", "__"), str(len(requested_states)))
                try:
                    target_count = int(target_raw)
                except (TypeError, ValueError):
                    target_count = -1
                if target_count < 0 or target_count > len(NIGERIA_STATES) - 1 or target_count != len(requested_states):
                    error = f"Select exactly the number of additional states requested for {listing['name']}."
                    break
                new_selections[listing["key"]] = {
                    "primary_state": listing["primary_state"],
                    "additional_states": requested_states,
                }
            if not error:
                subtotal = sum(len(new_selections[item["key"]]["additional_states"]) for item in listings) * PREMIUM_LISTING_RATE_NAIRA * duration
                vat = _premium_listing_vat(subtotal)
                session["premium_campaign_state_selections"] = new_selections
                session["premium_campaign_duration_months"] = duration
                session["premium_campaign_price_summary"] = {"subtotal": subtotal, "vat": vat, "total": subtotal + vat}
                session.modified = True
                saved = new_selections
                if request.form.get("continue_to_checkout") == "1":
                    return redirect(url_for("premium_listing_campaign_checkout"))
                flash("State coverage and duration saved. Your campaign estimate is ready.", "success")

    saved_duration = session.get("premium_campaign_duration_months", 1)
    if request.method == "GET" and saved_duration in PREMIUM_LISTING_DURATIONS:
        duration = saved_duration
    for listing in listings:
        selection = saved.get(listing["key"], {}) if isinstance(saved, dict) else {}
        listing["selected_states"] = selection.get("additional_states", [])

    summary = session.get("premium_campaign_price_summary", {})
    return render_template("premium_listing_campaign_states.html", user=user, listings=listings,
                           states=NIGERIA_STATES, durations=PREMIUM_LISTING_DURATIONS,
                           duration=duration, error=error, summary=summary)


def _premium_campaign_resolve_items(user):
    """Revalidate campaign item ownership and primary states from current server records."""
    business_ids = [str(x) for x in session.get("premium_campaign_selected_business_ids", [])]
    event_ids = [str(x) for x in session.get("premium_campaign_selected_event_ids", [])]
    resolved = []
    owned_ids = {str(item.get("business", {}).get("id")) for item in get_user_owned_businesses(user.get("id"))}
    valid_businesses = {str(b.get("id")): b for b in businesses.values() if is_public_business(b)}
    for item_id in business_ids:
        business = valid_businesses.get(item_id)
        if not business or item_id not in owned_ids:
            raise ValueError("A selected business is no longer available to your account. Please select your listings again.")
        raw_state = get_business_state(business)
        primary = next((state for state in NIGERIA_STATES if normalize_location(state) == normalize_location(raw_state)), "")
        if not primary:
            raise ValueError(f"We could not identify the primary state for {business.get('name', 'a selected business')}.")
        resolved.append({"key": "business:" + item_id, "kind": "business", "id": item_id,
                         "name": business.get("name", "Business"), "primary_state": primary})
    valid_events = {str(e.get("id")): e for e in event_submissions
                    if str(e.get("user_id")) == str(user.get("id")) and not e.get("deleted")
                    and str(e.get("status", "")).upper() not in {"DELETED", "REJECTED", "HIDDEN", "CANCELLED", "EXPIRED"}}
    for item_id in event_ids:
        event = valid_events.get(item_id)
        if not event:
            raise ValueError("A selected event is no longer available to your account. Please select your listings again.")
        raw_state = event.get("state") or event.get("state_of_event") or event.get("state_of_operation") or ""
        norm_state = normalize_location(raw_state)
        if norm_state in {"abuja", "fct", "fct abuja", "federal capital", "federal capital territory"}:
            norm_state = normalize_location("Federal Capital Territory")
        primary = next((state for state in NIGERIA_STATES if normalize_location(state) == norm_state), "")
        if not primary:
            raise ValueError(f"We could not identify the primary state for {event.get('title', 'a selected event')}.")
        resolved.append({"key": "event:" + item_id, "kind": "event", "id": item_id,
                         "name": event.get("title", "Event"), "primary_state": primary})
    if not resolved:
        raise ValueError("Choose your campaign listings before checkout.")
    saved = session.get("premium_campaign_state_selections", {})
    if not isinstance(saved, dict):
        raise ValueError("Your state selections expired. Please configure coverage again.")
    for item in resolved:
        choice = saved.get(item["key"])
        if not isinstance(choice, dict) or choice.get("primary_state") != item["primary_state"]:
            raise ValueError("Your state selections are out of date. Please review coverage again.")
        extras = choice.get("additional_states", [])
        if not isinstance(extras, list) or len(extras) > len(NIGERIA_STATES) - 1 or len(set(extras)) != len(extras):
            raise ValueError(f"Each listing can have up to {len(NIGERIA_STATES) - 1} unique additional states.")
        if any(state not in NIGERIA_STATES or state == item["primary_state"] for state in extras):
            raise ValueError("A selected state is invalid or duplicates a free primary state.")
        item["additional_states"] = extras
    return resolved


@app.route("/premium-listing/campaign/checkout", methods=["GET", "POST"])
def premium_listing_campaign_checkout():
    """Review a server-calculated multi-listing campaign and initialize one Paystack payment."""
    user = _premium_listing_user()
    if not user:
        flash("Please sign in before paying for a Premium Listing campaign.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_campaign_checkout")))
    try:
        items = _premium_campaign_resolve_items(user)
        duration = int(session.get("premium_campaign_duration_months", 0))
        if duration not in PREMIUM_LISTING_DURATIONS:
            raise ValueError("Choose a valid campaign duration before checkout.")
        state_months = sum(len(item["additional_states"]) for item in items)
        subtotal = state_months * PREMIUM_LISTING_RATE_NAIRA * duration
        vat = _premium_listing_vat(subtotal)
        total = subtotal + vat
        # Recompute session summary from trusted data; never trust browser totals.
        session["premium_campaign_price_summary"] = {"subtotal": subtotal, "vat": vat, "total": total,
                                                       "state_months": state_months, "duration_months": duration}
        session.modified = True
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("premium_listing_campaign_states"))

    if request.method == "POST":
        if total <= 0:
            flash("Select at least one additional state to create a paid campaign. Primary states are included free.", "error")
            return redirect(url_for("premium_listing_campaign_states"))
        email = (user.get("email") or "").strip()
        if not email:
            flash("Add an email address to your account before starting Paystack checkout.", "error")
            return redirect(url_for("account"))
        order_id = uuid.uuid4().hex
        reference = "ECO-CAMPAIGN-" + secrets.token_hex(12).upper()
        campaign_items = []
        for item in items:
            campaign_items.append({"key": item["key"], "kind": item["kind"], "id": item["id"],
                                   "name": item["name"], "primary_state": item["primary_state"],
                                   "additional_states": list(item["additional_states"])})
        order = {
            "id": order_id, "campaign_type": "multi_item", "user_id": user.get("id"),
            "reference": reference, "business_name": f"Premium campaign ({len(items)} listings)",
            "campaign_items": campaign_items, "listing_count": len(items), "state_months": state_months,
            "subtotal_naira": subtotal, "vat_rate": PREMIUM_LISTING_VAT_RATE, "vat_naira": vat,
            "amount_naira": total, "amount_kobo": total * 100,
            "rate_per_state_month": PREMIUM_LISTING_RATE_NAIRA, "duration_months": duration,
            "status": "INITIALIZING_PAYMENT", "payment_status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        premium_listing_orders.append(order)
        try:
            result = _paystack_request("/transaction/initialize", {
                "email": email, "amount": order["amount_kobo"], "reference": reference,
                "callback_url": url_for("premium_listing_payment_callback", _external=True),
                "metadata": {"purpose": "premium_listing_campaign", "order_id": order_id,
                             "user_id": str(user.get("id")), "listing_count": len(items),
                             "state_months": state_months, "duration_months": duration,
                             "subtotal_naira": subtotal, "vat_naira": vat, "vat_rate": PREMIUM_LISTING_VAT_RATE,
                             "total_naira": total}
            }, "POST")
            data = result.get("data") or {}
            if not result.get("status") or not data.get("authorization_url"):
                order["status"] = "PAYMENT_INITIALIZATION_FAILED"
                save_app_state()
                flash("Paystack could not start checkout. Please try again.", "error")
                return redirect(url_for("premium_listing_campaign_checkout"))
            order["status"] = "AWAITING_PAYMENT"
            order["access_code"] = data.get("access_code", "")
            save_app_state()
            return redirect(data["authorization_url"])
        except RuntimeError as exc:
            order["status"] = "PAYMENT_INITIALIZATION_FAILED"
            save_app_state()
            flash(str(exc), "error")
            return redirect(url_for("premium_listing_campaign_checkout"))

    return render_template("premium_listing_campaign_checkout.html", user=user, items=items,
                           duration=duration, duration_label=PREMIUM_LISTING_DURATIONS[duration],
                           state_months=state_months, subtotal=subtotal, vat=vat, total=total,
                           rate=PREMIUM_LISTING_RATE_NAIRA)


@app.route("/premium-listing/campaign/status/<order_id>")
def premium_listing_campaign_status(order_id):
    user = _premium_listing_user()
    if not user:
        return redirect(url_for("login", next=request.path))
    order = _premium_listing_order(order_id, user.get("id"))
    if not order or order.get("campaign_type") != "multi_item":
        flash("Premium campaign order not found.", "error")
        return redirect(url_for("premium_listing_plans"))
    return render_template("premium_listing_campaign_status.html", order=order)


@app.route("/premium-listing/apply", methods=["GET", "POST"])
def premium_listing_apply():
    user = _premium_listing_user()
    if not user:
        flash("Please sign in before applying for a Premium Business Listing.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_apply")))

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        category = request.form.get("category", "").strip()[:80]
        description = request.form.get("description", "").strip()[:1800]
        phone = request.form.get("phone", "").strip()[:40]
        email = request.form.get("email", "").strip()[:254]
        whatsapp = request.form.get("whatsapp", "").strip()[:80]
        instagram = request.form.get("instagram", "").strip()[:120]
        website = request.form.get("website", "").strip()[:300]
        address = request.form.get("address", "").strip()[:250]
        city = request.form.get("city", "").strip()[:100]
        lga = request.form.get("lga", "").strip()[:100]
        primary_state_raw = request.form.get("primary_state", "").strip()
        selected_states_raw = request.form.getlist("states")
        duration_raw = request.form.get("duration_months", "1").strip()
        valid_primary_state = next((state for state in NIGERIA_STATES
                                    if normalize_location(state) == normalize_location(primary_state_raw)), None)
        selected_states = []
        for raw_state in selected_states_raw:
            canonical = next((state for state in NIGERIA_STATES
                              if normalize_location(state) == normalize_location(raw_state)), None)
            if canonical and canonical not in selected_states:
                selected_states.append(canonical)
        try:
            duration_months = int(duration_raw)
        except (TypeError, ValueError):
            duration_months = 0

        errors = []
        if not all([name, category, description, phone, valid_primary_state, city, lga]):
            errors.append("Complete all required business and location details.")
        if not selected_states:
            errors.append("Select at least one state or the FCT for premium coverage.")
        if duration_months not in PREMIUM_LISTING_DURATIONS:
            errors.append("Choose a valid subscription duration.")
        if valid_primary_state and selected_states and valid_primary_state not in selected_states:
            errors.append("Include the business's primary state in your selected coverage.")
        if website and not website.startswith(("https://", "http://")):
            errors.append("Website URL must begin with http:// or https://.")

        image_paths, video_paths = [], []
        if not errors:
            try:
                for upload in request.files.getlist("business_images"):
                    if upload and upload.filename:
                        if len(image_paths) >= 8:
                            raise ValueError("Upload up to 8 business images.")
                        image_paths.append(_premium_upload(upload, PREMIUM_IMAGE_EXTENSIONS,
                                                           MAX_PREMIUM_IMAGE_SIZE, "Each image"))
                for upload in request.files.getlist("business_videos"):
                    if upload and upload.filename:
                        if len(video_paths) >= 3:
                            raise ValueError("Upload up to 3 business videos.")
                        video_paths.append(_premium_upload(upload, PREMIUM_VIDEO_EXTENSIONS,
                                                           MAX_PREMIUM_VIDEO_SIZE, "Each video"))
            except ValueError as exc:
                errors.append(str(exc))

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template("premium_listing_apply.html", user=user, states=NIGERIA_STATES,
                                   durations=PREMIUM_LISTING_DURATIONS, rate=PREMIUM_LISTING_RATE_NAIRA,
                                   vat_rate=PREMIUM_LISTING_VAT_RATE,
                                   form=request.form, selected_states=selected_states), 400

        subtotal_naira = len(selected_states) * PREMIUM_LISTING_RATE_NAIRA * duration_months
        vat_naira = _premium_listing_vat(subtotal_naira)
        amount_naira = subtotal_naira + vat_naira
        order_id = uuid.uuid4().hex
        reference = "ECO-LIST-" + secrets.token_hex(12).upper()
        order = {
            "id": order_id, "user_id": user.get("id"), "reference": reference,
            "subtotal_naira": subtotal_naira, "vat_rate": PREMIUM_LISTING_VAT_RATE,
            "vat_naira": vat_naira, "amount_naira": amount_naira,
            "amount_kobo": amount_naira * 100,
            "rate_per_state_month": PREMIUM_LISTING_RATE_NAIRA,
            "duration_months": duration_months, "selected_states": selected_states,
            "primary_state": valid_primary_state, "business_name": name, "category": category,
            "description": description, "phone": phone, "email": email or user.get("email", ""),
            "whatsapp": whatsapp, "instagram": instagram, "website": website,
            "address": address, "city": city, "lga": lga,
            "image_paths": image_paths, "video_paths": video_paths,
            "status": "INITIALIZING_PAYMENT", "payment_status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        premium_listing_orders.append(order)
        try:
            pay_email = (user.get("email") or email or "").strip()
            if not pay_email:
                raise RuntimeError("Your account needs an email address before checkout.")
            result = _paystack_request("/transaction/initialize", {
                "email": pay_email, "amount": order["amount_kobo"], "reference": reference,
                "callback_url": url_for("premium_listing_payment_callback", _external=True),
                "metadata": {"purpose": "premium_business_listing", "order_id": order_id,
                             "user_id": str(user.get("id")), "selected_state_count": len(selected_states),
                             "duration_months": duration_months,
                             "subtotal_naira": subtotal_naira, "vat_naira": vat_naira,
                             "vat_rate": PREMIUM_LISTING_VAT_RATE,
                             "total_naira": amount_naira}
            }, "POST")
            authorization_url = (result.get("data") or {}).get("authorization_url")
            if not result.get("status") or not authorization_url:
                order["status"] = "PAYMENT_INITIALIZATION_FAILED"
                save_app_state()
                flash("Paystack could not start checkout. Please try again.", "error")
                return redirect(url_for("premium_listing_apply"))
            order["status"] = "AWAITING_PAYMENT"
            order["access_code"] = (result.get("data") or {}).get("access_code", "")
            save_app_state()
            return redirect(authorization_url)
        except RuntimeError as exc:
            order["status"] = "PAYMENT_INITIALIZATION_FAILED"
            save_app_state()
            flash(str(exc), "error")
            return redirect(url_for("premium_listing_apply"))

    return render_template("premium_listing_apply.html", user=user, states=NIGERIA_STATES,
                           durations=PREMIUM_LISTING_DURATIONS, rate=PREMIUM_LISTING_RATE_NAIRA,
                           vat_rate=PREMIUM_LISTING_VAT_RATE,
                           form={}, selected_states=[])


@app.route("/premium-listing/payment/callback")
def premium_listing_payment_callback():
    reference = request.args.get("reference", "").strip()
    order = next((item for item in premium_listing_orders if item.get("reference") == reference), None)
    if not order:
        flash("We could not find this premium listing payment.", "error")
        return redirect(url_for("premium_listing_plans"))
    if str(order.get("user_id")) != str(session.get("user_id")):
        flash("Sign in to the account used for this payment to view its status.", "error")
        return redirect(url_for("login", next=url_for("premium_listing_payment_callback", reference=reference)))
    if order.get("payment_status") == "SUCCESS" and order.get("business_id"):
        return redirect(url_for("premium_listing_status", order_id=order["id"]))
    try:
        result = _paystack_request("/transaction/verify/" + urllib.parse.quote(reference, safe=""))
        data = result.get("data") or {}
        metadata = data.get("metadata") or {}
        valid = (result.get("status") and data.get("status") == "success"
                 and data.get("reference") == reference
                 and int(data.get("amount", -1)) == int(order.get("amount_kobo", -2))
                 and str(metadata.get("user_id", "")) == str(order.get("user_id"))
                 and str(metadata.get("order_id", "")) == str(order.get("id")))
        if not valid:
            order["payment_status"] = "FAILED_OR_UNVERIFIED"
            order["status"] = "PAYMENT_UNVERIFIED"
            save_app_state()
            flash("Payment could not be verified. Your listing has not been published.", "error")
            return redirect(url_for("premium_listing_status", order_id=order["id"]))
        order["payment_status"] = "SUCCESS"
        order["paystack_transaction_id"] = str(data.get("id", ""))
        order["channel"] = data.get("channel", "")
        order["verified_at"] = datetime.now(timezone.utc).isoformat()
        order["status"] = "PENDING_REVIEW"
        notify_admin("Premium campaign payment verified", f"Payment for premium campaign {order.get('id')} has been verified and is awaiting publishing review.", "premium_listing", order.get("id"))
        if order.get("campaign_type") == "multi_item":
            # Keep existing listings untouched until a moderator approves the paid campaign.
            save_app_state()
            flash("Payment verified. Your Premium campaign is awaiting admin review.", "success")
            return redirect(url_for("premium_listing_campaign_status", order_id=order["id"]))
        location = " · ".join(part for part in [order.get("address"), order.get("lga"), order.get("city"), order.get("primary_state")] if part)
        # A premium listing is an upgrade to an existing profile when the applicant
        # already owns/manages that business. Never create a second business record.
        business = _find_owned_business_for_premium_order(order)
        if business:
            business_id = business["id"]
            business.setdefault("premium_listing_orders", [])
            if order["id"] not in business["premium_listing_orders"]:
                business["premium_listing_orders"].append(order["id"])
            business.update({
                "premium_listing": True,
                "premium_listing_order_id": order["id"],
                "premium_states": order["selected_states"],
                "premium_duration_months": order["duration_months"],
                "premium_status": "PENDING_REVIEW",
            })
            # Fill only missing contact/profile fields; don't overwrite an approved listing.
            for key, value in (("phone", order.get("phone")), ("email", order.get("email")),
                               ("whatsapp", order.get("whatsapp")), ("instagram", order.get("instagram")),
                               ("website", order.get("website")), ("address", order.get("address"))):
                if value and not business.get(key):
                    business[key] = value
            existing_photos = business.setdefault("photos", [])
            for photo in order.get("image_paths", []):
                if photo not in existing_photos:
                    existing_photos.append(photo)
            existing_videos = business.setdefault("videos", [])
            for video in order.get("video_paths", []):
                if video not in existing_videos:
                    existing_videos.append(video)
            record_business_audit("PREMIUM_LISTING_APPLIED", business_id, order.get("user_id"),
                                  "Premium listing applied to existing business; no duplicate profile created.")
        else:
            business_id = max(businesses.keys(), default=0) + 1
            business = {
                "id": business_id, "name": order["business_name"], "category": order["category"],
                "location": location, "state": order["primary_state"], "city": order["city"],
                "lga": order["lga"], "address": order.get("address", ""),
                "description": order["description"], "about": order["description"],
                "submitted_by": order["user_id"], "primary_owner_id": order["user_id"],
                "manager_ids": [], "status": "PENDING REVIEW",
                "phone": order["phone"], "email": order.get("email", ""),
                "whatsapp": order.get("whatsapp", ""), "instagram": order.get("instagram", ""),
                "website": order.get("website", ""), "opening_hours": "",
                "photos": order.get("image_paths", []), "videos": order.get("video_paths", []),
                "premium_listing": True, "premium_listing_order_id": order["id"],
                "premium_states": order["selected_states"], "premium_duration_months": order["duration_months"],
                "premium_status": "PENDING_REVIEW"
            }
            businesses[business_id] = business
            submission_id = max((item.get("id", 0) for item in business_submissions), default=0) + 1
            order["submission_id"] = submission_id
            business_submissions.append({
                "id": submission_id, "user_id": order["user_id"], "business_id": business_id,
                "business_name": order["business_name"], "category": order["category"],
                "location": location, "state": order["primary_state"], "city": order["city"],
                "lga": order["lga"], "address": order.get("address", ""),
                "description": order["description"], "message": "Premium listing; payment verified.",
                "status": "PENDING", "premium_listing_order_id": order["id"]
            })
        order["business_id"] = business_id
        save_app_state()
        flash("Payment verified. Your premium business listing is now awaiting admin review.", "success")
        return redirect(url_for("premium_listing_status", order_id=order["id"]))
    except (RuntimeError, ValueError, TypeError) as exc:
        app.logger.warning("Premium listing payment verification issue: %s", exc)
        flash("We could not verify payment right now. Please check the status again later.", "error")
        return redirect(url_for("premium_listing_status", order_id=order["id"]))


@app.route("/premium-listing/status/<order_id>")
def premium_listing_status(order_id):
    user = _premium_listing_user()
    if not user:
        return redirect(url_for("login", next=request.path))
    order = _premium_listing_order(order_id, user.get("id"))
    if not order:
        flash("Premium listing application not found.", "error")
        return redirect(url_for("premium_listing_plans"))
    return render_template("premium_listing_status.html", order=order)


@app.route("/submit/business", methods=["GET", "POST"])
def submit_business():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = get_user_by_id(user_id)
    if user is None:
        session.pop("user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        if request.form.get("submission_type", "business").strip().lower() == "event":
            title = request.form.get("event_title", "").strip()
            category = request.form.get("event_category", "").strip()
            state_raw = request.form.get("event_state", "").strip()
            city = request.form.get("event_city", "").strip()
            lga = request.form.get("event_lga", "").strip()
            venue = request.form.get("event_venue", "").strip()
            event_date = request.form.get("event_date", "").strip()
            event_time = request.form.get("event_time", "").strip()
            host = request.form.get("event_host", "").strip()
            contact = request.form.get("event_contact", "").strip()
            ticket_price = request.form.get("event_ticket_price", "").strip() or "Not specified"
            flyer_url = request.form.get("event_flyer_url", "").strip()
            description = request.form.get("event_description", "").strip()
            flyer_file = request.files.get("event_flyer")

            valid_state = next(
                (item for item in NIGERIA_STATES if normalize_location(item) == normalize_location(state_raw)),
                None,
            )
            try:
                parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
            except ValueError:
                parsed_date = None

            if not all([title, category, valid_state, city, venue, event_time, host, contact, description]) or parsed_date is None:
                flash("Please complete all required event details and choose a valid event date.", "error")
                return redirect(url_for("submit_business"))
            if parsed_date < datetime.now().date():
                flash("The event date cannot be in the past.", "error")
                return redirect(url_for("submit_business"))
            if flyer_url and not re.match(r"^https?://", flyer_url, re.IGNORECASE):
                flash("Please enter a valid event flyer URL beginning with http:// or https://.", "error")
                return redirect(url_for("submit_business"))

            flyer_path = ""
            if flyer_file and flyer_file.filename:
                if not allowed_file(flyer_file.filename):
                    flash("Flyer must be a JPG, JPEG, PNG, or WEBP image.", "error")
                    return redirect(url_for("submit_business"))
                flyer_file.stream.seek(0, os.SEEK_END)
                flyer_size = flyer_file.stream.tell()
                flyer_file.stream.seek(0)
                if flyer_size > MAX_EVENT_FLYER_SIZE:
                    flash("Event flyer must be 5 MB or smaller.", "error")
                    return redirect(url_for("submit_business"))
                safe_name = secure_filename(flyer_file.filename)
                unique_name = f"event_{uuid.uuid4().hex}_{safe_name}"
                flyer_file.save(os.path.join(app.config["EVENT_FLYER_FOLDER"], unique_name))
                flyer_path = f"uploads/events/{unique_name}"

            event_id = max([item.get("id", 0) for item in event_submissions] + [item.get("id", 0) for item in EVENTS_DATA], default=0) + 1
            event_submissions.append({
                "id": event_id,
                "title": title,
                "category": category,
                "state": valid_state,
                "city": city,
                "lga": lga,
                "venue": venue,
                "location": " · ".join(part for part in [venue, city, valid_state] if part),
                "date": parsed_date.strftime("%b %d, %Y").upper(),
                "event_date": event_date,
                "time": datetime.strptime(event_time, "%H:%M").strftime("%I:%M %p").lstrip("0"),
                "event_time": event_time,
                "host": host,
                "contact": contact,
                "ticket_price": ticket_price,
                "flyer_url": flyer_url,
                "flyer_path": flyer_path,
                "description": description,
                "about": description,
                "icon": "📅",
                "user_id": user_id,
                "submitter_name": user.get("name", "Community member"),
                "status": "PENDING",
                "submitted_at": datetime.now().strftime("%b %d, %Y"),
            })
            notify_admin("Event submission awaiting review", f"{title} was submitted by {user.get('name', 'a user')} and is awaiting approval.", "event", event_id)
            save_app_state()
            flash("Event successfully created. Wait for approval.", "success")
            return redirect(url_for("submit_business"))

        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        state = request.form.get("state", "").strip()
        city = request.form.get("city", "").strip()
        lga = request.form.get("lga", "").strip()
        address = request.form.get("address", "").strip()
        description = request.form.get("description", "").strip()
        business_phone = request.form.get("business_phone", "").strip()
        business_email = request.form.get("business_email", "").strip().lower()
        message = request.form.get("message", "").strip()
        service_type = request.form.get("service_type", "").strip()

        if normalize_location(category) == "services":
            allowed_service_types = {normalize_location(item) for item in SERVICE_TYPES}
            if service_type and normalize_location(service_type) not in allowed_service_types:
                flash("Please choose a valid service type.", "error")
                return redirect(url_for("submit_business", category="Services"))
        else:
            service_type = ""

        # Keep compatibility with older versions of the form that only sent location.
        legacy_location = request.form.get("location", "").strip()
        if not state and legacy_location:
            state = legacy_location

        if not name or not category or not state or not city or not lga or not description or not (business_phone or business_email):
            return render_template(
                "submit_business.html",
                user=user,
                states=NIGERIA_STATES,
                now_date=datetime.now().strftime("%Y-%m-%d"),
                default_category=category,
                default_service_type=request.form.get("service_type", "").strip(),
                error="Complete the business name, category, state, city/town, LGA, description, and at least one business phone/email contact."
            )

        valid_state = next(
            (item for item in NIGERIA_STATES if normalize_location(item) == normalize_location(state)),
            None,
        )
        if valid_state is None:
            return render_template(
                "submit_business.html",
                user=user,
                states=NIGERIA_STATES,
                now_date=datetime.now().strftime("%Y-%m-%d"),
                error="Please select a valid Nigerian state or the Federal Capital Territory."
            )
        state = valid_state

        valid_lgas = NIGERIA_LGAS.get(state, [])
        if valid_lgas and not any(normalize_location(item) == normalize_location(lga) for item in valid_lgas):
            return render_template(
                "submit_business.html",
                user=user,
                states=NIGERIA_STATES,
                default_category=category,
                default_service_type=request.form.get("service_type", "").strip(),
                error="Please select an LGA from the list for your chosen state."
            )
        # If the dataset is missing/unreadable, keep the submitted LGA instead of
        # blocking registration; the form's local-data status can then be fixed.

        canonical_lga = next(
            (item for item in valid_lgas if normalize_location(item) == normalize_location(lga)),
            lga,
        )
        location_parts = [canonical_lga, city, state]
        if address:
            location_parts.insert(0, address)
        location = " · ".join(part for part in location_parts if part)

        normalized_name = normalize_location(name)
        normalized_location = normalize_location(location)
        duplicate = next((b for b in businesses.values() if not b.get("archived") and normalize_location(b.get("name", "")) == normalized_name and normalize_location(b.get("location", "")) == normalized_location), None)
        if duplicate:
            return render_template("submit_business.html", user=user, states=NIGERIA_STATES,
                now_date=datetime.now().strftime("%Y-%m-%d"), default_category=category,
                default_service_type=service_type,
                error=f"A business with this name and location already exists (ID {duplicate.get('id')}). Use the Claim Business option if you represent it.")

        new_business_id = max(businesses.keys(), default=0) + 1

        businesses[new_business_id] = {
            "id": new_business_id,
            "name": name,
            "category": category,
            "service_type": service_type,
            "location": location,
            "state": state,
            "city": city,
            "lga": canonical_lga,
            "address": address,
            "description": description,
            "about": description,
            "submitted_by": user_id,
            "primary_owner_id": user_id,
            "manager_ids": [],
            "status": "PENDING REVIEW",
            "phone": business_phone,
            "email": business_email,
            "whatsapp": "",
            "instagram": "",
            "website": "",
            "opening_hours": "",
            "photos": []
        }

        submission_id = max((int(item.get("id", 0)) for item in business_submissions), default=0) + 1
        business_submissions.append({
            "id": submission_id,
            "user_id": user_id,
            "business_id": new_business_id,
            "business_name": name,
            "category": category,
            "service_type": service_type,
            "location": location,
            "state": state,
            "city": city,
            "lga": canonical_lga,
            "address": address,
            "description": description,
            "phone": business_phone,
            "email": business_email,
            "message": message,
            "status": "PENDING",
            "created_at": _business_now()
        })
        business_claims.append({"id": max((int(item.get("id", 0)) for item in business_claims), default=0)+1,
            "user_id": user_id, "business_id": new_business_id, "business_name": name, "role": "Owner",
            "message": message or "Submitted with business contact evidence", "evidence_contact": business_phone or business_email,
            "evidence_details": "New business submission; contact details provided", "status": "PENDING",
            "claim_type": "INITIAL_OWNER", "created_at": _business_now()})
        record_business_audit("BUSINESS_SUBMITTED", new_business_id, user_id, f"Listing submitted with contact evidence; owner verification pending")
        notify_admin("Business listing awaiting review", f"{name} was submitted by {user.get('name', 'a user')} and is awaiting moderation.", "business_submission", submission_id)
        save_app_state()
        flash("Business submitted. Listing approval and ownership verification are tracked separately.", "success")
        return redirect(url_for("submit_business"))

    requested_category = request.args.get("category", "").strip()
    requested_service_type = request.args.get("service_type", "").strip()
    if normalize_location(requested_category) == "services":
        default_category = "Services"
    else:
        default_category = requested_category
    if requested_service_type not in SERVICE_TYPES:
        requested_service_type = ""
    return render_template(
        "submit_business.html", user=user, states=NIGERIA_STATES,
        now_date=datetime.now().strftime("%Y-%m-%d"),
        default_category=default_category, default_service_type=requested_service_type
    )


# =========================
# ADMIN — EVENT SUBMISSION REVIEW
# =========================

@app.route("/admin/events-review")
def admin_events_review():
    if not admin_required():
        return redirect(url_for("admin_login"))
    page = r"""
    <!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Event Review — THE ECOSYSTEM</title><style>
    *{box-sizing:border-box}body{margin:0;background:#080808;color:#f5f5f5;font:14px/1.6 Arial,sans-serif}
    header{padding:22px 5%;background:#111;border-bottom:1px solid #292929;display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap}
    a{color:#ff5353;text-decoration:none}main{max-width:1100px;margin:auto;padding:28px 18px 60px}h1{margin:0;font-size:clamp(25px,4vw,38px)}
    .item{background:#121212;border:1px solid #292929;border-radius:13px;padding:18px;margin:16px 0;overflow-wrap:anywhere}.muted{color:#999}.pill{color:#ff9a9a;border:1px solid #713030;border-radius:30px;padding:3px 9px;font-size:11px}
    button{border:0;border-radius:7px;padding:10px 14px;font-weight:bold;cursor:pointer}.approve{background:#176b42;color:#fff}.reject{background:#9f2525;color:#fff}form{display:inline-block;margin:10px 8px 0 0}.empty{border:1px dashed #444;padding:20px;border-radius:12px;color:#aaa}
    </style></head><body><header><div><div class="muted">THE ECOSYSTEM · ADMIN</div><h1>Event submissions</h1></div><nav><a href="{{ url_for('admin_dashboard') }}">Dashboard</a> · <a href="{{ url_for('admin_logout') }}">Log out</a></nav></header><main>
    {% with messages = get_flashed_messages(with_categories=true) %}{% for category, message in messages %}<p>{{ message }}</p>{% endfor %}{% endwith %}
    {% if event_submissions %}{% for event in event_submissions %}<article class="item"><span class="pill">{{ event.status }}</span><h2>{{ event.title }}</h2>
    <p class="muted">{{ event.category }} · {{ event.city }}, {{ event.state }} · {{ event.date }} at {{ event.time }}</p><p><strong>Venue:</strong> {{ event.venue }}</p><p><strong>Host:</strong> {{ event.host }} · <strong>Contact:</strong> {{ event.contact }}</p>
    <p><strong>Ticket:</strong> {{ event.ticket_price }}</p><p>{{ event.description }}</p>{% if event.flyer_path %}<p><strong>Uploaded flyer:</strong><br><a href="{{ url_for('static', filename=event.flyer_path) }}" target="_blank" rel="noopener"><img src="{{ url_for('static', filename=event.flyer_path) }}" alt="Flyer for {{ event.title }}" style="display:block;max-width:100%;width:320px;max-height:420px;object-fit:contain;margin-top:8px;border-radius:10px;border:1px solid #333"></a></p>{% endif %}{% if event.flyer_url %}<p><a href="{{ event.flyer_url }}" target="_blank" rel="noopener">Open event flyer link</a></p>{% endif %}<p class="muted">Submitted by {{ event.submitter_name }} · {{ event.submitted_at }}</p>
    {% if event.status in ['PENDING', 'REJECTED'] %}<form method="post" action="{{ url_for('review_event_submission', event_id=event.id, decision='approve') }}"><button class="approve">Approve & Publish</button></form>{% endif %}{% if event.status != 'REJECTED' %}<form method="post" action="{{ url_for('review_event_submission', event_id=event.id, decision='reject') }}"><button class="reject">Reject</button></form>{% endif %}{% if event.status == 'APPROVED' %}<form method="post" action="{{ url_for('review_event_submission', event_id=event.id, decision='hide') }}"><button class="reject">Hide from Public</button></form>{% endif %}{% if event.status == 'HIDDEN' %}<form method="post" action="{{ url_for('review_event_submission', event_id=event.id, decision='restore') }}"><button class="approve">Restore</button></form>{% endif %}<form method="post" action="{{ url_for('review_event_submission', event_id=event.id, decision='delete') }}" onsubmit="return confirm('Permanently delete this event? This cannot be undone.');"><button class="reject">Delete Permanently</button></form></article>{% endfor %}{% else %}<div class="empty">No event submissions yet.</div>{% endif %}
    </main></body></html>"""
    refresh_expired_event_statuses()
    return render_template_string(page, event_submissions=sorted(event_submissions, key=lambda item: item.get("id", 0), reverse=True))


@app.route("/admin/event/<int:event_id>/<decision>", methods=["POST"])
def review_event_submission(event_id, decision):
    if not admin_required():
        return redirect(url_for("admin_login"))
    event = next((item for item in event_submissions if item.get("id") == event_id), None)
    if event is None:
        flash("Event submission not found.", "error")
    elif decision == "approve":
        if event_is_expired(event):
            event["status"] = "EXPIRED"
            flash("This event date has passed, so it cannot be published.", "error")
        else:
            event["status"] = "APPROVED"
            event["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
            notify_user_action(event.get("user_id"), "Event approved and published", f"Your event {event.get('title', 'event')} has been approved and is now published on THE ECOSYSTEM.", "event", event.get("id"))
            flash("Event approved and published. Submitter notified.", "success")
    elif decision == "reject":
        event["status"] = "REJECTED"
        event["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
        notify_user_action(event.get("user_id"), "Event submission not approved", f"Your event {event.get('title', 'event')} was not approved for publication. Please review the submission details and contact support if you need clarification.", "event", event.get("id"))
        flash("Event rejected. Submitter notified.", "success")
    elif decision == "hide":
        event["status"] = "HIDDEN"
        event["hidden_at"] = datetime.now().strftime("%b %d, %Y")
        flash("Event hidden from public listings.", "success")
    elif decision == "restore":
        if event_is_expired(event):
            event["status"] = "EXPIRED"
            flash("This event has expired and cannot be restored to public listings.", "error")
        else:
            event["status"] = "APPROVED"
            event["restored_at"] = datetime.now().strftime("%b %d, %Y")
            notify_user_action(event.get("user_id"), "Event restored", f"Your event {event.get('title', 'event')} has been restored to public listings.", "event", event.get("id"))
            flash("Event restored to public listings. Submitter notified.", "success")
    elif decision == "delete":
        if event.get("flyer_path"):
            flyer_full_path = os.path.join(PROJECT_FOLDER, "static", event["flyer_path"])
            try:
                if os.path.isfile(flyer_full_path):
                    os.remove(flyer_full_path)
            except OSError:
                app.logger.warning("Could not remove event flyer: %s", flyer_full_path)
        event_submissions.remove(event)
        flash("Event permanently deleted.", "success")
    else:
        flash("Invalid review action.", "error")
    save_app_state()
    return redirect(url_for("admin_events_review"))


# =========================
# ADMIN LOGIN / AUTH
# =========================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        return render_template("admin_login.html", error="Incorrect admin email or password.")

    return render_template("admin_login.html")


def admin_required():
    return session.get("admin_logged_in") is True


def record_admin_activity(endpoint, action, target="", status_code=200):
    """Record a concise, non-sensitive audit event for admin operations."""
    entry = {
        "id": max((int(item.get("id", 0)) for item in admin_activity_log), default=0) + 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": "ADMIN",
        "endpoint": str(endpoint or "unknown")[:120],
        "action": str(action or "submitted")[:100],
        "target": str(target or "—")[:180],
        "status_code": int(status_code),
    }
    admin_activity_log.append(entry)
    if len(admin_activity_log) > 5000:
        del admin_activity_log[:-5000]


@app.after_request
def capture_admin_activity(response):
    """Capture successful admin POST actions without recording form contents."""
    if (request.path.startswith("/admin/") and request.method == "POST"
            and session.get("admin_logged_in") is True
            and response.status_code < 400
            and request.endpoint not in {"admin_activity_history"}):
        endpoint = request.endpoint or request.path
        action = request.form.get("action") or request.form.get("decision") or endpoint
        target_parts = []
        for key in ("business_id", "submission_id", "claim_id", "event_id", "entry_id",
                    "campaign_id", "product_id", "review_id", "report_id", "user_id",
                    "item_id", "kind", "selected_ids", "selected_items"):
            values = request.form.getlist(key)
            if values:
                target_parts.append(f"{key}=" + ",".join(str(value)[:40] for value in values[:8]))
        if not target_parts and request.view_args:
            for key, value in request.view_args.items():
                if key.endswith("_id") or key == "item_id":
                    target_parts.append(f"{key}={str(value)[:40]}")
        record_admin_activity(endpoint, action, " · ".join(target_parts) or request.path, response.status_code)
        try:
            save_app_state()
        except Exception:
            app.logger.exception("Could not persist admin activity history")
    return response


# =========================
# ADMIN DASHBOARD
# =========================

@app.route("/admin/activity")
def admin_activity_history():
    if not admin_required():
        return redirect(url_for("admin_login"))
    entries = sorted(admin_activity_log, key=lambda item: item.get("created_at", ""), reverse=True)
    query = str(request.args.get("q", "")).strip().lower()
    if query:
        entries = [item for item in entries if query in " ".join(str(item.get(key, "")) for key in ("actor", "endpoint", "action", "target", "created_at")).lower()]
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = 50
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    entries = entries[(page - 1) * per_page:page * per_page]
    return render_template("admin_activity.html", entries=entries, query=query, total=total, page=page, pages=pages)


@app.route("/admin")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))

    pending_claims = []
    for claim in business_claims:
        if claim.get("status") != "PENDING":
            continue

        pending_claims.append({
            "claim": claim,
            "business": businesses.get(claim["business_id"]),
            "user": get_user_by_id(claim["user_id"])
        })

    pending_submissions = []
    for submission in business_submissions:
        if submission.get("status") != "PENDING":
            continue

        pending_submissions.append({
            "submission": submission,
            "business": businesses.get(submission["business_id"]),
            "user": get_user_by_id(submission["user_id"])
        })

    pending_competition_entries = []
    for entry in competition_entries:
        if entry.get("status") != "PENDING":
            continue

        pending_competition_entries.append({
            "entry": entry,
            "user": get_user_by_id(entry.get("user_id")),
            "competition": get_competition_by_id(entry.get("competition_id"))
        })

    _expire_spotlight_campaigns()
    refresh_expired_event_statuses()
    # Normalize statuses so the overview does not miss records stored with
    # different capitalization or legacy status spellings.
    def _status_is(record, *expected):
        status = str(record.get("status", "")).strip().upper().replace(" ", "_")
        return status in {value.upper().strip().replace(" ", "_") for value in expected}

    dashboard_stats = {
        "users": len(users),
        "businesses": len(businesses),
        "public_businesses": sum(1 for item in businesses.values() if is_public_business(item)),
        "events": len(EVENTS_DATA) + len(event_submissions),
        "approved_events": sum(1 for item in event_submissions if _status_is(item, "APPROVED")),
        "pending_events": sum(1 for item in event_submissions if _status_is(item, "PENDING", "PENDING REVIEW", "PENDING_REVIEW")),
        "marketplace_listings": len(marketplace_products),
        "approved_listings": sum(1 for item in marketplace_products if _status_is({"status": item.get("status", "APPROVED")}, "APPROVED")),
        "pending_listings": sum(1 for item in marketplace_products if _status_is(item, "PENDING", "PENDING REVIEW", "PENDING_REVIEW", "PENDING APPROVAL")),
        "reviews": len(reviews),
        "open_reports": sum(1 for item in reports if _status_is({"status": item.get("status", "OPEN")}, "OPEN", "PENDING", "NEW", "UNRESOLVED")),
        "creators": len(CREATORS_DATA) if "CREATORS_DATA" in globals() else 0,
        "competitions": len(competitions_data),
        "pending_spotlight": sum(1 for item in spotlight_campaigns if _status_is(item, "PENDING_REVIEW", "PENDING", "PENDING REVIEW")),
        "active_spotlight": sum(1 for item in spotlight_campaigns if _status_is(item, "APPROVED", "ACTIVE")),
        "successful_spotlight_payments": sum(1 for item in spotlight_payments if _status_is(item, "SUCCESS")),
        "premium_listing_orders": len(premium_listing_orders),
        "pending_premium_listings": sum(1 for item in premium_listing_orders if _status_is(item, "PENDING_REVIEW", "PENDING", "PENDING REVIEW")),
        "active_premium_listings": sum(1 for item in premium_listing_orders if _status_is(item, "ACTIVE", "APPROVED")),
        "pending_claims": len(pending_claims),
        "pending_submissions": len(pending_submissions),
        "pending_competition_entries": len(pending_competition_entries),
    }
    dashboard_stats["attention_total"] = sum(dashboard_stats[key] for key in (
        "pending_submissions", "pending_claims", "pending_events", "pending_listings",
        "open_reports", "pending_spotlight", "pending_competition_entries",
        "pending_premium_listings",
    ))

    # Revenue overview passed explicitly to the dashboard template.
    successful_revenue_records = []
    for payment in spotlight_payments:
        status = str(payment.get("status", "")).strip().upper()
        if status in {"SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED", "VERIFIED"}:
            try:
                amount = float(payment.get("amount_naira", payment.get("amount", 0)) or 0)
                if amount > 1000000 and payment.get("amount_naira") is None:
                    amount = amount / 100
                successful_revenue_records.append((payment, amount))
            except (TypeError, ValueError):
                pass
    for order in premium_listing_orders:
        status = str(order.get("payment_status", order.get("status", ""))).strip().upper()
        if status in {"SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED", "VERIFIED", "ACTIVE"}:
            try:
                amount = float(order.get("amount_naira", order.get("amount", order.get("price", 0))) or 0)
                if amount > 1000000 and order.get("amount_naira") is None:
                    amount = amount / 100
                successful_revenue_records.append((order, amount))
            except (TypeError, ValueError):
                pass
    now_utc = datetime.now(timezone.utc)
    month_revenue = 0.0
    for record, amount in successful_revenue_records:
        date_value = str(record.get("paid_at", record.get("verified_at", record.get("created_at", ""))))
        try:
            paid_date = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            if paid_date.tzinfo is None:
                paid_date = paid_date.replace(tzinfo=timezone.utc)
            if paid_date.year == now_utc.year and paid_date.month == now_utc.month:
                month_revenue += amount
        except (ValueError, TypeError):
            pass
    revenue_overview = {
        "gross_verified_naira": sum(amount for _, amount in successful_revenue_records),
        "this_month_naira": month_revenue,
        "successful_count": len(successful_revenue_records),
    }

    return render_template(
        "admin.html",
        revenue_overview=revenue_overview,
        pending_claims=pending_claims,
        pending_submissions=pending_submissions,
        pending_competition_entries=pending_competition_entries,
        competitions=competitions_data,
        dashboard_stats=dashboard_stats,
        recent_admin_activity=sorted(admin_activity_log, key=lambda item: item.get("created_at", ""), reverse=True)[:6]
    )


@app.route("/admin/revenue")
def admin_revenue():
    """Show verified revenue and recorded payment attempts for admin reconciliation."""
    if not admin_required():
        return redirect(url_for("admin_login"))

    successful_statuses = {"SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED", "VERIFIED", "ACTIVE"}
    pending_statuses = {"PENDING", "INITIALIZING", "INITIALIZING_PAYMENT", "AWAITING_PAYMENT", "PENDING_REVIEW"}
    failed_statuses = {"FAILED", "FAILURE", "FAILED_OR_UNVERIFIED", "INITIALIZATION_FAILED", "PAYMENT_INITIALIZATION_FAILED", "PAYMENT_UNVERIFIED", "REJECTED", "CANCELLED", "CANCELED"}

    def _amount_naira(record, *, spotlight=False):
        value = record.get("amount_naira")
        if value is None:
            value = record.get("amount")
        if value is None and spotlight:
            value = record.get("amount_kobo", 0)
            try:
                return float(value or 0) / 100
            except (TypeError, ValueError):
                return 0.0
        if value is None:
            value = record.get("price", record.get("total_naira", 0))
        try:
            amount = float(value or 0)
            # Legacy Paystack records sometimes store the amount only in kobo.
            if record.get("amount_naira") is None and record.get("amount_kobo") is None and amount > 1_000_000:
                amount /= 100
            return max(0.0, amount)
        except (TypeError, ValueError):
            return 0.0

    def _date_value(record):
        return str(record.get("verified_at") or record.get("paid_at") or record.get("created_at") or "")

    transactions = []
    for payment in spotlight_payments:
        user = get_user_by_id(payment.get("user_id"))
        status = str(payment.get("status", "UNKNOWN")).strip().upper()
        plan = SPOTLIGHT_PLANS.get(payment.get("plan_id"), {}) if "SPOTLIGHT_PLANS" in globals() else {}
        transactions.append({
            "description": str(plan.get("name") or payment.get("plan_id") or "Business Spotlight"),
            "customer": str((user or {}).get("email") or (user or {}).get("name") or payment.get("user_id") or "Unknown customer"),
            "source": "Business Spotlight", "reference": str(payment.get("reference", "")),
            "channel": str(payment.get("channel") or "Paystack"),
            "amount_naira": _amount_naira(payment, spotlight=True), "status": status,
            "date": _date_value(payment), "_source": "spotlight",
        })

    for order in premium_listing_orders:
        user = get_user_by_id(order.get("user_id"))
        status = str(order.get("payment_status") or order.get("status") or "UNKNOWN").strip().upper()
        transactions.append({
            "description": str(order.get("business_name") or "Premium business listing"),
            "customer": str(order.get("email") or (user or {}).get("email") or (user or {}).get("name") or order.get("user_id") or "Unknown customer"),
            "source": "Premium Listings", "reference": str(order.get("reference", "")),
            "channel": str(order.get("channel") or "Paystack"),
            "amount_naira": _amount_naira(order), "status": status,
            "date": _date_value(order), "_source": "premium",
        })

    transactions.sort(key=lambda item: item.get("date", ""), reverse=True)
    successful = [t for t in transactions if t["status"] in successful_statuses]
    pending = [t for t in transactions if t["status"] in pending_statuses]
    failed = [t for t in transactions if t["status"] in failed_statuses or t not in successful and t not in pending]
    now = datetime.now(timezone.utc)
    month_buckets = []
    for offset in range(5, -1, -1):
        month_index = now.month - 1 - offset
        year = now.year + month_index // 12
        month = month_index % 12 + 1
        month_buckets.append({"year": year, "month": month, "label": datetime(year, month, 1).strftime("%b"), "amount": 0.0, "height": 5})
    month_lookup = {(item["year"], item["month"]): item for item in month_buckets}
    for transaction in successful:
        try:
            paid_date = datetime.fromisoformat(transaction["date"].replace("Z", "+00:00"))
            if paid_date.tzinfo is None:
                paid_date = paid_date.replace(tzinfo=timezone.utc)
            bucket = month_lookup.get((paid_date.year, paid_date.month))
            if bucket:
                bucket["amount"] += transaction["amount_naira"]
        except (ValueError, TypeError):
            continue
    max_month_amount = max((item["amount"] for item in month_buckets), default=0)
    for item in month_buckets:
        item["height"] = max(5, round(item["amount"] / max_month_amount * 100)) if max_month_amount else 5

    spotlight_total = sum(t["amount_naira"] for t in successful if t["_source"] == "spotlight")
    premium_total = sum(t["amount_naira"] for t in successful if t["_source"] == "premium")
    current_month_total = month_lookup.get((now.year, now.month), {}).get("amount", 0.0)
    revenue = {
        "gross_verified_naira": spotlight_total + premium_total,
        "this_month_naira": current_month_total,
        "successful_count": len(successful), "pending_count": len(pending),
        "failed_count": len(failed), "spotlight_naira": spotlight_total,
        "premium_listing_naira": premium_total, "months": month_buckets,
        "updated_at": now.strftime("%d %b %Y, %H:%M UTC"),
        "transactions": [{k: v for k, v in t.items() if not k.startswith("_")} for t in transactions[:250]],
    }
    return render_template("admin_revenue.html", revenue=revenue)


# =========================
# ADMIN — SYSTEM HEALTH MONITOR
# =========================

@app.route("/admin/system-health")
def admin_system_health():
    if not admin_required():
        return redirect(url_for("admin_login"))

    checks = []
    database_ok = False
    database_detail = "Database check failed."
    database_size = 0
    try:
        with connect_database() as connection:
            connection.execute("SELECT 1").fetchone()
            row = connection.execute(
                "SELECT length(data) FROM app_state WHERE id = 1"
            ).fetchone()
            database_ok = True
            database_size = int(row[0] or 0) if row else 0
            database_detail = ("PostgreSQL" if DATABASE_URL else "SQLite") + (" connection is working; saved state record is present." if row else " works, but no saved application state row was found.")
    except Exception as exc:
        database_detail = f"{type(exc).__name__}: database connection failed."
        app.logger.exception("Admin system health database check failed")

    checks.append({
        "name": "PostgreSQL database" if DATABASE_URL else "SQLite database",
        "status": "healthy" if database_ok and database_size > 0 else ("warning" if database_ok else "error"),
        "detail": database_detail,
    })

    upload_paths = [
        ("Business uploads", app.config.get("UPLOAD_FOLDER", UPLOAD_FOLDER)),
        ("Event uploads", app.config.get("EVENT_FLYER_FOLDER", EVENT_FLYER_FOLDER)),
        ("Premium listing uploads", app.config.get("PREMIUM_MEDIA_FOLDER", PREMIUM_MEDIA_FOLDER)),
    ]
    for label, path in upload_paths:
        try:
            os.makedirs(path, exist_ok=True)
            writable = os.access(path, os.W_OK)
        except OSError:
            writable = False
        checks.append({
            "name": label,
            "status": "healthy" if writable else "error",
            "detail": "Folder exists and is writable." if writable else "Folder is missing or not writable.",
        })

    resend_configured = bool(os.environ.get("RESEND_API_KEY", "").strip() and os.environ.get("RESEND_FROM", "onboarding@resend.dev").strip())
    checks.append({
        "name": "Email configuration",
        "status": "healthy" if resend_configured and not HEALTH_LAST_EMAIL_ERROR else ("warning" if resend_configured else "error"),
        "detail": (
            "Resend credentials are configured. This does not guarantee delivery to every recipient."
            if resend_configured and not HEALTH_LAST_EMAIL_ERROR
            else (HEALTH_LAST_EMAIL_ERROR or "RESEND_API_KEY is missing; outgoing email cannot be sent.")
        ),
    })

    location_count = sum(len(items) for items in NIGERIA_LGAS.values()) if isinstance(NIGERIA_LGAS, dict) else 0
    checks.append({
        "name": "Nigeria location dataset",
        "status": "healthy" if location_count > 0 else "warning",
        "detail": f"{len(NIGERIA_LGAS)} states mapped with {location_count} LGAs." if location_count else "No LGAs were loaded. Check static/data/nigeria-lga-clean.json.",
    })

    pending_counts = [
        ("Business submissions", sum(1 for x in business_submissions if str(x.get("status", "")).strip().upper().replace(" ", "_") in {"PENDING", "PENDING_REVIEW"})),
        ("Ownership claims", sum(1 for x in business_claims if str(x.get("status", "")).strip().upper() in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW"})),
        ("Event submissions", sum(1 for x in event_submissions if str(x.get("status", "")).strip().upper() in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW"})),
        ("Open reports", sum(1 for x in reports if str(x.get("status", "OPEN")).strip().upper() in {"OPEN", "PENDING", "NEW", "UNRESOLVED"})),
        ("Marketplace listings", sum(1 for x in marketplace_products if str(x.get("status", "APPROVED")).strip().upper() in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW", "PENDING APPROVAL"})),
    ]
    total_pending = sum(count for _, count in pending_counts)
    health_summary = {
        "database_path": DATABASE_PATH,
        "database_size": database_size,
        "last_save_at": HEALTH_LAST_SAVE_AT,
        "last_save_error": HEALTH_LAST_SAVE_ERROR,
        "last_email_success_at": HEALTH_LAST_EMAIL_SUCCESS_AT,
        "last_email_error": HEALTH_LAST_EMAIL_ERROR,
        "pending_counts": pending_counts,
        "total_pending": total_pending,
        "users": len(users),
        "businesses": len(businesses),
        "reviews": len(reviews),
        "checks_healthy": sum(1 for item in checks if item["status"] == "healthy"),
        "checks_total": len(checks),
    }
    return render_template("admin_system_health.html", checks=checks, health=health_summary)


# =========================
# ADMIN — OPEN / CLOSE VOTING
# =========================

@app.route(
    "/admin/competition/<int:competition_id>/voting",
    methods=["POST"]
)
def admin_competition_voting(competition_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    competition = get_competition_by_id(competition_id)
    if competition is None:
        flash("Competition not found.", "error")
        return redirect(url_for("admin_dashboard"))

    requested_status = request.form.get("voting_status", "").strip().upper()

    if requested_status not in ("OPEN", "CLOSED"):
        flash("Invalid voting status.", "error")
        return redirect(url_for("admin_dashboard"))

    if requested_status == "OPEN":
        approved_entries = get_approved_competition_entries(competition_id)

        if not approved_entries:
            flash(
                f"Approve at least one nomination for {competition['title']} before opening voting.",
                "error"
            )
            return redirect(url_for("admin_dashboard"))

    competition["voting_status"] = requested_status

    if requested_status == "OPEN":
        flash(f"Voting is now OPEN for {competition['title']}.", "success")
    else:
        flash(f"Voting is now CLOSED for {competition['title']}.", "success")

    return redirect(url_for("admin_dashboard"))


# =========================
# ADMIN — BUSINESS SUBMISSIONS
# =========================

@app.route("/admin/submission/<int:submission_id>/approve", methods=["POST"])
def approve_submission(submission_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    submission = next(
        (item for item in business_submissions if item.get("id") == submission_id),
        None
    )

    if submission is None:
        return "Business submission not found", 404

    business = businesses.get(submission["business_id"])
    if business is None:
        return "Business not found", 404

    premium_order_id = submission.get("premium_listing_order_id")
    if premium_order_id:
        premium_order = _premium_listing_order(premium_order_id)
        if not premium_order or premium_order.get("payment_status") != "SUCCESS":
            flash("Cannot approve this premium listing until its payment is verified.", "error")
            return redirect(url_for("admin_dashboard"))
        now = datetime.now(timezone.utc)
        premium_order.update({"status": "ACTIVE", "approved_at": now.isoformat(), "starts_at": now.isoformat(),
                              "expires_at": (now + timedelta(days=30 * int(premium_order.get("duration_months", 1)))).isoformat()})
        business.update({"premium_status": "ACTIVE", "premium_starts_at": premium_order["starts_at"],
                         "premium_expires_at": premium_order["expires_at"]})
    submission["status"] = "APPROVED"
    business["status"] = "APPROVED"

    owner_id = submission.get("user_id")
    claim = get_business_owner_claim(business["id"], owner_id)

    if claim is None:
        business_claims.append({
            "id": max((item.get("id", 0) for item in business_claims), default=0) + 1,
            "user_id": owner_id,
            "business_id": business["id"],
            "business_name": business["name"],
            "role": "Owner",
            "message": submission.get("message", "Business submitted for review."),
            "status": "APPROVED"
        })
    else:
        claim["status"] = "APPROVED"
        claim["business_name"] = business["name"]

    record_business_audit("BUSINESS_LISTING_APPROVED", business["id"], "ADMIN", "Submission approved via admin dashboard")
    notify_business_user(owner_id, "Business listing approved", f"{business.get('name')} has been approved for public discovery.", business["id"])
    save_app_state()
    flash("Business submission approved. Listing status updated; ownership verification is tracked separately.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/submission/<int:submission_id>/reject", methods=["POST"])
def reject_submission(submission_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    submission = next(
        (item for item in business_submissions if item.get("id") == submission_id),
        None
    )

    if submission is None:
        return "Business submission not found", 404

    submission["status"] = "REJECTED"
    business = businesses.get(submission["business_id"])
    premium_order_id = submission.get("premium_listing_order_id")
    if premium_order_id:
        premium_order = _premium_listing_order(premium_order_id)
        if premium_order:
            premium_order.update({"status": "REJECTED", "reviewed_at": datetime.now(timezone.utc).isoformat()})

    if business:
        business["status"] = "REJECTED"
        if business.get("premium_listing"):
            business["premium_status"] = "REJECTED"

    record_business_audit("BUSINESS_LISTING_REJECTED", business.get("id") if business else None, "ADMIN", "Submission rejected via admin dashboard")
    notify_business_user(submission.get("user_id"), "Business listing rejected", f"{submission.get('business_name', 'Your business')} was rejected. Review the requirements and resubmit if appropriate.", submission.get("business_id"))
    save_app_state()
    flash("Business submission rejected.", "success")
    return redirect(url_for("admin_dashboard"))


# =========================
# ADMIN — BUSINESS CLAIMS
# =========================

@app.route("/admin/claim/<int:claim_id>/approve", methods=["POST"])
def approve_claim(claim_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    claim = next((item for item in business_claims if item["id"] == claim_id), None)
    if claim is None:
        return "Claim not found", 404

    business = businesses.get(claim["business_id"])
    if not business:
        return "Business not found", 404
    if claim.get("role") == "Owner" or claim.get("claim_type") == "INITIAL_OWNER":
        current_owner = business.get("primary_owner_id", business.get("submitted_by"))
        if current_owner and str(current_owner) != str(claim.get("user_id")) and not business.get("ownership_dispute"):
            flash("A primary owner already exists. Use the ownership transfer/dispute process.", "error")
            return redirect(url_for("admin_dashboard"))
        business["primary_owner_id"] = claim.get("user_id")
        business["submitted_by"] = claim.get("user_id")
    else:
        managers = business.setdefault("manager_ids", [])
        if str(claim.get("user_id")) not in {str(x) for x in managers}: managers.append(claim.get("user_id"))
    claim["status"] = "APPROVED"; claim["reviewed_at"] = _business_now()
    record_business_audit("OWNERSHIP_CLAIM_APPROVED", business.get("id"), "ADMIN", f"Claim {claim.get('id')} approved")
    notify_business_user(claim.get("user_id"), "Business claim approved", f"Your claim for {business.get('name')} has been approved. Listing visibility is managed separately.", business.get("id"))
    save_app_state()
    flash("Business claim approved. Listing publication status was not changed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/claim/<int:claim_id>/reject", methods=["POST"])
def reject_claim(claim_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    claim = next((item for item in business_claims if item["id"] == claim_id), None)
    if claim is None:
        return "Claim not found", 404

    claim["status"] = "REJECTED"; claim["reviewed_at"] = _business_now()
    business = businesses.get(claim["business_id"])
    record_business_audit("OWNERSHIP_CLAIM_REJECTED", claim.get("business_id"), "ADMIN", f"Claim {claim.get('id')} rejected")
    notify_business_user(claim.get("user_id"), "Business claim rejected", f"Your claim for {claim.get('business_name', 'a business')} was rejected. You may submit new evidence if you believe this was an error.", claim.get("business_id"))
    save_app_state()
    flash("Business claim rejected. Listing status remains unchanged.", "success")
    return redirect(url_for("admin_dashboard"))


# =========================
# P9 — ADMIN COMPETITION ENTRY REVIEW
# =========================

@app.route("/admin/competition-entries")
def admin_competition_entries():
    if not admin_required():
        return redirect(url_for("admin_login"))

    selected_status = str(request.args.get("status", "PENDING")).strip().upper()
    allowed_statuses = {"PENDING", "APPROVED", "REJECTED", "DELETED"}
    if selected_status not in allowed_statuses:
        selected_status = "PENDING"

    entries = []
    deleted_entries = []
    if selected_status == "DELETED":
        for record in sorted(
            (item for item in deleted_items
             if item.get("scope") == "ADMIN" and item.get("kind") == "competition_entry"),
            key=lambda item: item.get("deleted_at", ""), reverse=True
        ):
            entry = record.get("item", {})
            deleted_entries.append({
                "trash": record,
                "entry": entry,
                "user": get_user_by_id(entry.get("user_id")),
                "competition": get_competition_by_id(entry.get("competition_id")),
            })
    else:
        for entry in competition_entries:
            status = str(entry.get("status", "PENDING")).strip().upper()
            if status != selected_status:
                continue
            entries.append({
                "entry": entry,
                "user": get_user_by_id(entry.get("user_id")),
                "competition": get_competition_by_id(entry.get("competition_id")),
            })
        entries.sort(key=lambda item: item["entry"].get("id", 0), reverse=True)

    counts = {
        status: sum(1 for entry in competition_entries
                    if str(entry.get("status", "PENDING")).strip().upper() == status)
        for status in ("PENDING", "APPROVED", "REJECTED")
    }
    counts["DELETED"] = sum(
        1 for item in deleted_items
        if item.get("scope") == "ADMIN" and item.get("kind") == "competition_entry"
    )
    return render_template(
        "competition_admin_entries.html",
        entries=entries,
        deleted_entries=deleted_entries,
        selected_status=selected_status,
        counts=counts,
    )


@app.route("/admin/competition-entries/bulk-action", methods=["POST"])
def admin_competition_entries_bulk_action():
    if not admin_required():
        return redirect(url_for("admin_login"))

    selected_status = str(request.form.get("status", "PENDING")).strip().upper()
    if selected_status not in {"PENDING", "APPROVED", "REJECTED", "DELETED"}:
        selected_status = "PENDING"
    action = str(request.form.get("action", "delete")).strip().lower()
    selected_ids = request.form.getlist("selected_ids")
    if not selected_ids:
        flash("Select at least one competition entry first.", "error")
        return redirect(url_for("admin_competition_entries", status=selected_status))

    outcomes = []
    if selected_status == "DELETED":
        if action == "restore":
            for trash_id in selected_ids:
                record = next((item for item in deleted_items
                               if str(item.get("id")) == str(trash_id)
                               and item.get("scope") == "ADMIN"
                               and item.get("kind") == "competition_entry"), None)
                outcomes.append(_restore_trash_item(trash_id, "ADMIN") if record else (False, "Deleted competition entry not found."))
        elif action == "delete":
            selected = set(str(value) for value in selected_ids)
            before = len(deleted_items)
            deleted_items[:] = [item for item in deleted_items if not (
                item.get("scope") == "ADMIN" and item.get("kind") == "competition_entry"
                and str(item.get("id")) in selected
            )]
            outcomes = [(True, "Permanently deleted.")] * (before - len(deleted_items))
        else:
            flash("Unsupported action.", "error")
            return redirect(url_for("admin_competition_entries", status=selected_status))
    elif action == "delete":
        for entry_id in selected_ids:
            entry = get_competition_entry_by_id(entry_id)
            if entry is None or str(entry.get("status", "PENDING")).upper() != selected_status:
                outcomes.append((False, "Entry not found in this status page."))
                continue
            outcomes.append(_trash_item("competition_entry", entry_id, scope="ADMIN", actor_id="ADMIN"))
    else:
        flash("Unsupported action.", "error")
        return redirect(url_for("admin_competition_entries", status=selected_status))

    save_app_state()
    succeeded = sum(1 for ok, _ in outcomes if ok)
    failed = len(outcomes) - succeeded
    if succeeded:
        message = f"Processed {succeeded} competition entr{'y' if succeeded == 1 else 'ies'}."
        if selected_status != "DELETED" and action == "delete":
            message = f"Moved {succeeded} selected competition entr{'y' if succeeded == 1 else 'ies'} to Deleted Entries."
        flash(message + (f" {failed} item(s) could not be processed." if failed else ""), "success")
    else:
        flash("No competition entries were changed. Please refresh and try again.", "error")
    return redirect(url_for("admin_competition_entries", status=selected_status))


@app.route("/admin/competition-entry/<int:entry_id>/approve", methods=["POST"])
def approve_competition_entry(entry_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    entry = get_competition_entry_by_id(entry_id)
    if entry is None:
        return "Competition entry not found", 404

    entry["status"] = "APPROVED"
    entry["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
    notify_user_action(entry.get("user_id"), "Competition entry approved", f"Your entry {entry.get('entrant_name', 'submission')} for {entry.get('competition_title', 'the competition')} has been approved and is now eligible for public display/voting where applicable.", "competition_entry", entry.get("id"))
    save_app_state()
    flash("Competition entry approved and made public.", "success")
    return redirect(url_for("admin_competition_entries", status="APPROVED"))


@app.route("/admin/competition-entry/<int:entry_id>/reject", methods=["POST"])
def reject_competition_entry(entry_id):
    if not admin_required():
        return redirect(url_for("admin_login"))

    entry = get_competition_entry_by_id(entry_id)
    if entry is None:
        return "Competition entry not found", 404

    entry["status"] = "REJECTED"
    entry["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
    notify_user_action(entry.get("user_id"), "Competition entry not approved", f"Your entry {entry.get('entrant_name', 'submission')} for {entry.get('competition_title', 'the competition')} was not approved. Please contact support if you need clarification.", "competition_entry", entry.get("id"))
    competition_votes[:] = [vote for vote in competition_votes if vote["entry_id"] != entry_id]
    save_app_state()
    flash("Competition entry rejected.", "success")
    return redirect(url_for("admin_competition_entries", status="REJECTED"))



# =========================
# ADMIN — CENTRAL MODERATOR WORKSPACE
# =========================



@app.route("/business/<int:business_id>/ownership-action", methods=["POST"])
def business_ownership_action(business_id):
    user_id = session.get("user_id")
    if user_id is None: return redirect(url_for("login"))
    business = businesses.get(business_id)
    if not business or not business_has_access(business, user_id): return "Business not found or access denied", 404
    action = request.form.get("action", "").strip().lower()
    primary = str(business.get("primary_owner_id", business.get("submitted_by", ""))) == str(user_id)
    if action == "archive":
        if not primary: flash("Only the primary owner can archive a business.", "error")
        else:
            business["archived"] = True; business["status"] = "ARCHIVED"
            record_business_audit("BUSINESS_ARCHIVED", business_id, user_id, "Primary owner archived listing")
            notify_business_user(user_id, "Business archived", f"{business.get('name')} is archived. Its records are preserved.", business_id)
            flash("Business archived and removed from discovery. Its records are preserved.", "success")
    elif action == "restore":
        if not primary: flash("Only the primary owner can request restoration.", "error")
        else:
            business["restore_requested"] = True
            record_business_audit("BUSINESS_RESTORATION_REQUESTED", business_id, user_id, "Primary owner requested restoration")
            flash("Restoration requested. Admin will check listing and ownership requirements.", "success")
    elif action == "transfer":
        if not primary: flash("Only the primary owner can initiate ownership transfer.", "error")
        elif business.get("ownership_dispute"): flash("Transfer is paused while an ownership dispute is active.", "error")
        else:
            target_email = request.form.get("target_email", "").strip().lower()
            target = next((u for u in users if (u.get("email") or "").strip().lower() == target_email), None)
            if not target: flash("No registered account matches that email.", "error")
            else:
                business["ownership_transfer_request"] = {"from_user_id": user_id, "to_user_id": target.get("id"), "status": "PENDING", "created_at": _business_now()}
                record_business_audit("OWNERSHIP_TRANSFER_REQUESTED", business_id, user_id, f"Transfer requested to user {target.get('id')}")
                notify_business_user(target.get("id"), "Ownership transfer requested", f"The primary owner of {business.get('name')} requested a transfer. Admin approval is required.", business_id)
                flash("Ownership transfer request submitted for admin review.", "success")
    elif action == "manager":
        if not primary: flash("Only the primary owner can request manager changes.", "error")
        elif business.get("ownership_dispute"): flash("Manager changes are paused during an ownership dispute.", "error")
        else:
            target_email = request.form.get("target_email", "").strip().lower()
            target = next((u for u in users if (u.get("email") or "").strip().lower() == target_email), None)
            if not target: flash("No registered account matches that email.", "error")
            else:
                business.setdefault("manager_change_requests", []).append({"user_id": target.get("id"), "action": request.form.get("manager_action", "add"), "status": "PENDING", "requested_by": user_id, "created_at": _business_now()})
                record_business_audit("MANAGER_CHANGE_REQUESTED", business_id, user_id, f"Manager change requested for user {target.get('id')}")
                notify_business_user(target.get("id"), "Manager access request", f"A manager access change for {business.get('name')} is awaiting admin review.", business_id)
                flash("Manager change submitted for admin approval.", "success")
    elif action == "dispute":
        reason = request.form.get("reason", "").strip()
        if not reason: flash("Describe the ownership dispute.", "error")
        else:
            business["ownership_dispute"] = True; business["ownership_dispute_reason"] = reason
            record_business_audit("OWNERSHIP_DISPUTE_OPENED", business_id, user_id, reason)
            flash("Ownership dispute recorded. Ownership changes and new listings are paused pending admin review.", "success")
    else: flash("Unsupported business action.", "error")
    save_app_state()
    return redirect(url_for("business_dashboard"))

def _cleanup_read_notifications():
    """Delete read notifications 48 hours after they were opened."""
    now = datetime.now(timezone.utc)
    kept = []
    changed = False
    for item in business_notifications:
        if item.get("read"):
            read_stamp = item.get("read_at") or item.get("created_at")
            try:
                stamp = datetime.fromisoformat(str(read_stamp).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if now - stamp.astimezone(timezone.utc) >= timedelta(hours=48):
                    changed = True
                    continue
            except (TypeError, ValueError):
                # Keep records with an unparseable timestamp rather than deleting unexpectedly.
                pass
        kept.append(item)
    if changed:
        business_notifications[:] = kept
    return changed


def _current_user_notifications():
    user_id = session.get("user_id")
    _cleanup_read_notifications()
    return sorted(
        [n for n in business_notifications if str(n.get("user_id")) == str(user_id)],
        key=lambda n: n.get("created_at", ""), reverse=True
    )


@app.route("/api/notifications", methods=["GET"])
def api_notifications():
    user_id = session.get("user_id")
    if user_id is None:
        return jsonify({"authenticated": False, "login_url": url_for("login"), "notifications": [], "unread": 0}), 401
    changed = _cleanup_read_notifications()
    items = _current_user_notifications()
    if changed:
        save_app_state()
    return jsonify({
        "authenticated": True,
        "unread": sum(1 for item in items if not item.get("read")),
        "notifications": [{
            "id": item.get("id"), "title": item.get("title", "Notification"),
            "message": item.get("message", ""), "read": bool(item.get("read")),
            "created_at": item.get("created_at", ""),
            "read_at": item.get("read_at", "")
        } for item in items[:50]]
    })


@app.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
def api_notification_read(notification_id):
    user_id = session.get("user_id")
    if user_id is None:
        return jsonify({"error": "Please log in to view notifications."}), 401
    _cleanup_read_notifications()
    item = next((n for n in business_notifications if int(n.get("id", -1)) == notification_id and str(n.get("user_id")) == str(user_id)), None)
    if item is None:
        return jsonify({"error": "Notification not found."}), 404
    if not item.get("read"):
        item["read"] = True
        item["read_at"] = _business_now()
    elif not item.get("read_at"):
        item["read_at"] = _business_now()
    save_app_state()
    return jsonify({"success": True, "unread": sum(1 for n in business_notifications if str(n.get("user_id")) == str(user_id) and not n.get("read")), "read_at": item.get("read_at")})


@app.route("/api/notifications/<int:notification_id>", methods=["DELETE"])
def api_notification_delete(notification_id):
    user_id = session.get("user_id")
    if user_id is None:
        return jsonify({"error": "Please log in to manage notifications."}), 401
    index = next((i for i, n in enumerate(business_notifications) if int(n.get("id", -1)) == notification_id and str(n.get("user_id")) == str(user_id)), None)
    if index is None:
        return jsonify({"error": "Notification not found."}), 404
    del business_notifications[index]
    save_app_state()
    return jsonify({"success": True, "unread": sum(1 for n in business_notifications if str(n.get("user_id")) == str(user_id) and not n.get("read"))})


@app.route("/api/notifications/clear", methods=["POST"])
def api_notifications_clear():
    user_id = session.get("user_id")
    if user_id is None:
        return jsonify({"error": "Please log in to manage notifications."}), 401
    business_notifications[:] = [n for n in business_notifications if str(n.get("user_id")) != str(user_id)]
    save_app_state()
    return jsonify({"success": True, "unread": 0})


@app.route("/api/admin/notifications", methods=["GET"])
def api_admin_notifications():
    if not session.get("admin_logged_in"):
        return jsonify({"authenticated": False, "login_url": url_for("admin_login"), "notifications": [], "unread": 0}), 401
    changed = _cleanup_read_notifications()
    items = sorted([n for n in business_notifications if str(n.get("user_id")) == "ADMIN"],
                   key=lambda n: n.get("created_at", ""), reverse=True)
    if changed:
        save_app_state()
    return jsonify({"authenticated": True, "unread": sum(1 for n in items if not n.get("read")),
                    "notifications": [{"id": n.get("id"), "title": n.get("title", "Notification"),
                                       "message": n.get("message", ""), "read": bool(n.get("read")),
                                       "created_at": n.get("created_at", "")} for n in items[:50]]})


@app.route("/api/admin/notifications/<int:notification_id>/read", methods=["POST"])
def api_admin_notification_read(notification_id):
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Admin sign-in required."}), 401
    item = next((n for n in business_notifications if int(n.get("id", -1)) == notification_id
                 and str(n.get("user_id")) == "ADMIN"), None)
    if item is None:
        return jsonify({"error": "Notification not found."}), 404
    item["read"] = True
    item["read_at"] = _business_now()
    save_app_state()
    return jsonify({"success": True})


@app.route("/api/admin/notifications/<int:notification_id>", methods=["DELETE"])
def api_admin_notification_delete(notification_id):
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Admin sign-in required."}), 401
    index = next((i for i, n in enumerate(business_notifications) if int(n.get("id", -1)) == notification_id
                  and str(n.get("user_id")) == "ADMIN"), None)
    if index is None:
        return jsonify({"error": "Notification not found."}), 404
    del business_notifications[index]
    save_app_state()
    return jsonify({"success": True})


@app.route("/api/admin/notifications/clear", methods=["POST"])
def api_admin_notifications_clear():
    if not session.get("admin_logged_in"):
        return jsonify({"error": "Admin sign-in required."}), 401
    business_notifications[:] = [n for n in business_notifications if str(n.get("user_id")) != "ADMIN"]
    save_app_state()
    return jsonify({"success": True})


@app.route("/account/notifications/read", methods=["POST"])
def mark_business_notifications_read():
    user_id = session.get("user_id")
    if user_id is None: return redirect(url_for("login"))
    for item in business_notifications:
        if str(item.get("user_id")) == str(user_id):
            item["read"] = True
            item.setdefault("read_at", _business_now())
    save_app_state()
    return redirect(url_for("account"))

def _trash_collection(kind):
    """Return the live collection that owns a trashable record."""
    return {
        "business": businesses,
        "event": event_submissions,
        "product": marketplace_products,
        "spotlight": spotlight_campaigns,
        "premium_listing": premium_listing_orders,
        "business_notification": business_notifications,
        "review": reviews,
        "report": reports,
        "claim": business_claims,
        "user": users,
        "competition_entry": competition_entries,
        "competition_vote": competition_votes,
        "competition": competitions_data,
    }.get(kind)


def _record_id(item):
    return item.get("id", item.get("business_id")) if isinstance(item, dict) else None


def _find_live_item(kind, item_id):
    collection = _trash_collection(kind)
    if collection is None:
        return None
    if kind == "business":
        try:
            return collection.get(int(item_id))
        except (TypeError, ValueError):
            return None
    return next((x for x in collection if str(_record_id(x)) == str(item_id)), None)


def _trash_item(kind, item_id, scope="ADMIN", actor_id=None):
    """Move a record into recoverable trash instead of destroying it."""
    global marketplace_premium_ad
    item = _find_live_item(kind, item_id)
    if item is None:
        return False, "Record not found."
    owner_id = item.get("user_id", item.get("submitted_by", item.get("primary_owner_id")))
    if scope == "USER":
        owns_item = str(owner_id) == str(actor_id)
        if kind == "business" and isinstance(item, dict):
            owns_item = business_has_access(item, actor_id)
        if not owns_item:
            return False, "You can only delete records belonging to your account."
        # Trash is scoped to the account that requested the deletion, including approved claim owners.
        owner_id = actor_id
    record = {
        "id": uuid.uuid4().hex,
        "scope": scope,
        "kind": kind,
        "original_id": _record_id(item),
        "owner_id": owner_id,
        "deleted_by": actor_id,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "item": json.loads(json.dumps(item)),
    }
    if kind == "business":
        record["original_id"] = int(item.get("id", item_id))
        businesses.pop(int(item.get("id", item_id)), None)
    else:
        collection = _trash_collection(kind)
        collection.remove(item)
    if kind == "spotlight" and marketplace_premium_ad.get("campaign_id") == item.get("id"):
        record["previous_marketplace_ad"] = json.loads(json.dumps(marketplace_premium_ad))
        marketplace_premium_ad = {}
    deleted_items.append(record)
    return True, "Moved to Deleted Items. You can restore it from the Deleted page."


def _restore_trash_item(trash_id, scope, actor_id=None):
    global marketplace_premium_ad
    record = next((x for x in deleted_items if str(x.get("id")) == str(trash_id) and x.get("scope") == scope), None)
    if record is None:
        return False, "Deleted record not found."
    if scope == "USER" and str(record.get("owner_id")) != str(actor_id):
        return False, "You can only restore records belonging to your account."
    kind = record.get("kind")
    item = json.loads(json.dumps(record.get("item", {})))
    collection = _trash_collection(kind)
    if collection is None:
        return False, "This record type cannot be restored automatically."
    original_id = record.get("original_id")
    if kind == "business":
        try:
            key = int(original_id)
        except (TypeError, ValueError):
            return False, "The business ID is invalid; record was kept in trash."
        if key in businesses:
            return False, "A live business already uses that ID; record was kept in trash."
        businesses[key] = item
    else:
        if any(str(_record_id(x)) == str(original_id) for x in collection):
            return False, "A live record already uses that ID; record was kept in trash."
        collection.append(item)
    if record.get("previous_marketplace_ad") and not marketplace_premium_ad:
        marketplace_premium_ad = record["previous_marketplace_ad"]
    deleted_items.remove(record)
    return True, "Record restored successfully."


@app.route("/account/bulk-delete", methods=["POST"])
def account_bulk_delete():
    """Move selected account-owned items into the user's recoverable Deleted Items."""
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))

    selected = request.form.getlist("selected_items")
    if not selected:
        flash("Select at least one item first.", "error")
        return redirect(url_for("account"))

    allowed_kinds = {"premium_listing", "business_notification", "business", "event"}
    outcomes = []
    seen = set()
    for raw in selected[:200]:
        try:
            kind, item_id = raw.split(":", 1)
        except ValueError:
            outcomes.append((False, "Invalid selection."))
            continue
        key = (kind, item_id)
        if key in seen:
            continue
        seen.add(key)
        if kind not in allowed_kinds or not item_id:
            outcomes.append((False, "Unsupported selection."))
            continue

        item = _find_live_item(kind, item_id)
        if item is None:
            outcomes.append((False, "One selected item was not found."))
            continue

        # Explicitly enforce account ownership before moving anything to trash.
        if kind == "business":
            owns_item = business_has_access(item, user_id)
        else:
            owns_item = str(item.get("user_id")) == str(user_id)
        if not owns_item:
            outcomes.append((False, "You can only delete items belonging to your account."))
            continue

        outcomes.append(_trash_item(kind, item_id, scope="USER", actor_id=user_id))

    save_app_state()
    succeeded = sum(1 for ok, _ in outcomes if ok)
    failed = len(outcomes) - succeeded
    if succeeded:
        flash(f"Moved {succeeded} selected item(s) to Deleted Items. You can restore them there." + (f" {failed} item(s) could not be moved." if failed else ""), "success")
    else:
        flash("No items were deleted. Check that the selected records still belong to your account.", "error")
    return redirect(url_for("account"))


@app.route("/admin/deleted")
def admin_deleted_items():
    if not admin_required():
        return redirect(url_for("admin_login"))
    records = sorted([x for x in deleted_items if x.get("scope") == "ADMIN"], key=lambda x: x.get("deleted_at", ""), reverse=True)
    return render_template("deleted_items_admin.html", deleted_items=records)


@app.route("/account/deleted")
def account_deleted_items():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))
    records = sorted([x for x in deleted_items if x.get("scope") == "USER" and str(x.get("owner_id")) == str(user_id)], key=lambda x: x.get("deleted_at", ""), reverse=True)
    return render_template("deleted_items_account.html", deleted_items=records)


@app.route("/admin/deleted/action", methods=["POST"])
def admin_deleted_action():
    if not admin_required():
        return redirect(url_for("admin_login"))
    action = request.form.get("action", "restore")
    ids = request.form.getlist("selected_ids")
    if request.form.get("trash_id"):
        ids = [request.form.get("trash_id")]
    if action == "restore":
        outcomes = [_restore_trash_item(i, "ADMIN") for i in ids]
    elif action == "delete":
        wanted = set(ids)
        before = len(deleted_items)
        deleted_items[:] = [x for x in deleted_items if not (x.get("scope") == "ADMIN" and str(x.get("id")) in wanted)]
        outcomes = [(True, "Deleted permanently.")] * (before - len(deleted_items))
    else:
        outcomes = [(False, "Unsupported action.")]
    save_app_state()
    success = sum(1 for ok, _ in outcomes if ok)
    flash(f"Processed {success} selected item(s).", "success" if success else "error")
    return redirect(url_for("admin_deleted_items"))


@app.route("/account/deleted/action", methods=["POST"])
def account_deleted_action():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))
    action = request.form.get("action", "restore")
    ids = request.form.getlist("selected_ids")
    if request.form.get("trash_id"):
        ids = [request.form.get("trash_id")]
    if action == "restore":
        outcomes = [_restore_trash_item(i, "USER", user_id) for i in ids]
    elif action == "delete":
        wanted = set(ids)
        owned = [x for x in deleted_items if x.get("scope") == "USER" and str(x.get("owner_id")) == str(user_id) and str(x.get("id")) in wanted]
        for record in owned:
            deleted_items.remove(record)
        outcomes = [(True, "Deleted permanently.")] * len(owned)
    else:
        outcomes = [(False, "Unsupported action.")]
    save_app_state()
    success = sum(1 for ok, _ in outcomes if ok)
    flash(f"Processed {success} selected item(s).", "success" if success else "error")
    return redirect(url_for("account_deleted_items"))


@app.route("/admin/bulk-action", methods=["POST"])
def admin_bulk_action():
    global marketplace_premium_ad
    if not admin_required():
        return redirect(url_for("admin_login"))
    kind = request.form.get("kind", "")
    action = request.form.get("action", "delete")
    ids = request.form.getlist("selected_ids")
    if not ids:
        flash("Select at least one item first.", "error")
        return redirect(url_for("admin_moderator") + "#" + request.form.get("tab", "businesses"))
    done = 0
    for item_id in ids:
        if action == "delete":
            ok, _ = _trash_item(kind, item_id, "ADMIN", "ADMIN")
            done += bool(ok)
        elif action == "hide":
            item = _find_live_item(kind, item_id)
            if item is not None and isinstance(item, dict):
                if kind in {"business", "event", "product", "spotlight", "premium_listing", "review", "report", "claim", "competition_entry"}:
                    item["_admin_previous_status"] = item.get("status", "APPROVED")
                    if kind == "premium_listing":
                        item["admin_hidden"] = True
                        business = businesses.get(item.get("business_id"))
                        if business:
                            business["_admin_previous_premium_status"] = business.get("premium_status", "ACTIVE")
                            business["premium_status"] = "HIDDEN"
                    else:
                        item["status"] = "HIDDEN"
                    if kind == "spotlight" and marketplace_premium_ad.get("campaign_id") == item.get("id"):
                        item["_admin_previous_marketplace_ad"] = json.loads(json.dumps(marketplace_premium_ad))
                        marketplace_premium_ad = {}
                    done += 1
        elif action == "restore":
            item = _find_live_item(kind, item_id)
            if item is not None and isinstance(item, dict) and kind in {"business", "event", "product", "spotlight", "premium_listing", "review", "report", "claim", "competition_entry"}:
                if kind == "premium_listing":
                    item["admin_hidden"] = False
                    business = businesses.get(item.get("business_id"))
                    if business:
                        business["premium_status"] = business.pop("_admin_previous_premium_status", "ACTIVE")
                else:
                    item["status"] = item.pop("_admin_previous_status", "APPROVED")
                if kind == "spotlight" and item.get("_admin_previous_marketplace_ad") and not marketplace_premium_ad:
                    marketplace_premium_ad = item.pop("_admin_previous_marketplace_ad")
                done += 1
    save_app_state()
    flash(f"Processed {done} of {len(ids)} selected {kind.replace('_', ' ')} item(s).", "success" if done else "error")
    return redirect(url_for("admin_moderator") + "#" + request.form.get("tab", "businesses"))


@app.route("/admin/moderator/bulk-delete", methods=["POST"])
def admin_moderator_bulk_delete():
    """Move multiple selected moderator records to recoverable admin trash."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    selected = request.form.getlist("selected_items")
    business_status_filter = str(request.form.get("business_status", "ALL")).strip().upper()
    if business_status_filter not in {"ALL", "PENDING", "APPROVED", "REJECTED", "HIDDEN", "ARCHIVED"}:
        business_status_filter = "ALL"
    redirect_target = url_for("admin_moderator", business_status=business_status_filter) + "#businesses"
    if not selected:
        flash("Select at least one item first.", "error")
        return redirect(redirect_target)

    done = 0
    failed = 0
    seen = set()
    for value in selected:
        try:
            kind, item_id = value.split("|", 1)
        except ValueError:
            failed += 1
            continue
        if kind not in {"business", "event", "product", "spotlight", "premium_listing", "business_notification", "review", "report", "claim", "user", "competition_entry", "competition_vote", "competition"}:
            failed += 1
            continue
        key = (kind, str(item_id))
        if key in seen:
            continue
        seen.add(key)
        ok, _message = _trash_item(kind, item_id, "ADMIN", "ADMIN")
        done += int(bool(ok))
        failed += int(not ok)

    save_app_state()
    flash(f"Moved {done} selected item(s) to Deleted Items." + (f" {failed} item(s) could not be deleted." if failed else ""), "success" if done else "error")
    return redirect(redirect_target)


@app.route("/account/bulk-action", methods=["POST"])
def account_bulk_action():
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login"))
    kind = request.form.get("kind", "")
    action = request.form.get("action", "delete")
    ids = request.form.getlist("selected_ids")
    if not ids:
        flash("Select at least one item first.", "error")
        return redirect(url_for("account"))
    done = 0
    for item_id in ids:
        if action == "delete":
            ok, _ = _trash_item(kind, item_id, "USER", user_id)
            done += bool(ok)
        elif action in {"hide", "restore"}:
            item = _find_live_item(kind, item_id)
            owner_id = item.get("user_id", item.get("submitted_by", item.get("primary_owner_id"))) if item else None
            authorized = item is not None and str(owner_id) == str(user_id)
            if item is not None and kind == "business":
                authorized = authorized or str(item.get("primary_owner_id")) == str(user_id) or str(user_id) in {str(x) for x in item.get("manager_ids", [])}
            if item is not None and authorized and kind in {"business", "event", "product"}:
                if action == "hide":
                    item["_user_previous_status"] = item.get("status", "APPROVED")
                    item["status"] = "HIDDEN"
                else:
                    item["status"] = item.pop("_user_previous_status", "APPROVED")
                done += 1
    save_app_state()
    flash(f"Processed {done} selected item(s).", "success" if done else "error")
    return redirect(url_for("account"))


@app.route("/admin/moderator")
def admin_moderator():
    if not admin_required():
        return redirect(url_for("admin_login"))
    _expire_spotlight_campaigns()
    refresh_expired_event_statuses()
    requested_business_status = str(request.args.get("business_status", "ALL")).strip().upper()
    if requested_business_status not in {"ALL", "PENDING", "APPROVED", "REJECTED", "HIDDEN", "ARCHIVED"}:
        requested_business_status = "ALL"

    business_items = []
    for business in sorted(businesses.values(), key=lambda item: item.get("id", 0), reverse=True):
        business_copy = dict(business)
        raw_status = str(business.get("status", "")).strip().upper()
        linked_submissions = [s for s in business_submissions if str(s.get("business_id")) == str(business.get("id"))]
        submission_statuses = [str(s.get("status", "")).strip().upper().replace(" ", "_") for s in linked_submissions]
        if raw_status in {"HIDDEN", "ARCHIVED", "DISPUTED", "REJECTED", "PENDING", "PENDING REVIEW", "PENDING_REVIEW"}:
            moderation_status = raw_status.replace(" ", "_")
        elif any(status in {"REJECTED", "DECLINED"} for status in submission_statuses):
            moderation_status = "REJECTED"
        elif any(status in {"PENDING", "PENDING_REVIEW", "UNDER_REVIEW"} for status in submission_statuses):
            moderation_status = "PENDING"
        elif linked_submissions and all(status == "APPROVED" for status in submission_statuses):
            moderation_status = "APPROVED"
        else:
            moderation_status = raw_status.replace(" ", "_") or "APPROVED"
        business_copy["status"] = moderation_status
        if requested_business_status != "ALL" and moderation_status != requested_business_status:
            continue
        linked_claims = [c for c in business_claims if str(c.get("business_id")) == str(business.get("id")) and str(c.get("status", "PENDING")).upper() == "PENDING"]
        business_items.append({"business": business_copy, "review_count": len(get_business_reviews(business["id"])),
            "moderation_status": moderation_status,
            "claims": linked_claims,
            "reports": [r for r in reports if r.get("target_type") == "business" and str(r.get("target_id")) == str(business.get("id")) and r.get("status", "OPEN") == "OPEN"]})
    review_items = []
    for review in sorted(reviews, key=lambda item: item.get("id", 0), reverse=True):
        review_items.append({"review": review, "business": businesses.get(review.get("business_id")),
            "reports": [r for r in reports if r.get("target_type") == "review" and str(r.get("target_id")) == str(review.get("id")) and r.get("status", "OPEN") == "OPEN"]})
    return render_template("admin_moderator.html", businesses=business_items,
        business_status_filter=requested_business_status,
        business_status_counts={
            "ALL": len(businesses),
            "PENDING": sum(1 for b in businesses.values() if str(b.get("status", "")).strip().upper() in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW"} or any(str(x.get("business_id")) == str(b.get("id")) and str(x.get("status", "")).strip().upper().replace(" ", "_") in {"PENDING", "PENDING_REVIEW"} for x in business_submissions)),
            "APPROVED": sum(1 for b in businesses.values() if str(b.get("status", "")).strip().upper() == "APPROVED" or (not b.get("status") and not any(str(x.get("business_id")) == str(b.get("id")) and str(x.get("status", "")).strip().upper() in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW", "REJECTED"} for x in business_submissions))),
            "REJECTED": sum(1 for b in businesses.values() if str(b.get("status", "")).strip().upper() == "REJECTED" or any(str(x.get("business_id")) == str(b.get("id")) and str(x.get("status", "")).strip().upper() == "REJECTED" for x in business_submissions)),
            "HIDDEN": sum(1 for b in businesses.values() if str(b.get("status", "")).strip().upper() == "HIDDEN"),
            "ARCHIVED": sum(1 for b in businesses.values() if str(b.get("status", "")).strip().upper() == "ARCHIVED"),
        },
        events=sorted(event_submissions, key=lambda x: x.get("id", 0), reverse=True),
        products=sorted(marketplace_products, key=lambda x: x.get("id", 0), reverse=True),
        campaigns=sorted(spotlight_campaigns, key=lambda x: x.get("created_at", ""), reverse=True),
        plans=SPOTLIGHT_PLANS, reviews=review_items,
        reports=sorted(reports, key=lambda x: x.get("created_at", ""), reverse=True),
        users=users, users_by_id={str(u.get("id")): u for u in users},
        competition_entries=sorted(competition_entries, key=lambda x: x.get("id", 0), reverse=True),
        competition_votes=sorted(competition_votes, key=lambda x: x.get("id", 0), reverse=True),
        businesses_by_id=businesses, competitions=competitions_data,
        ownership_claims=sorted([c for c in business_claims if c.get("status") == "PENDING"], key=lambda c: c.get("created_at", ""), reverse=True),
        business_audit_log=sorted(business_audit_log, key=lambda x: x.get("created_at", ""), reverse=True)[:100],
        premium_orders=sorted(premium_listing_orders, key=lambda o: o.get("created_at", ""), reverse=True),
        marketplace_ad=marketplace_premium_ad,
        stats={"businesses": len(businesses), "events": len(event_submissions), "products": len(marketplace_products),
               "campaigns": len(spotlight_campaigns), "reviews": len(reviews),
               "open_reports": sum(1 for r in reports if r.get("status", "OPEN") == "OPEN")})


@app.route("/admin/moderator/action/<kind>/<item_id>/<action>", methods=["POST"])
def admin_moderator_action(kind, item_id, action):
    item_id_raw = str(item_id)
    try:
        item_id_int = int(item_id_raw)
    except (TypeError, ValueError):
        item_id_int = None
    if not admin_required():
        return redirect(url_for("admin_login"))
    allowed = {"approve", "reject", "hide", "restore", "archive", "delete", "resolve", "clear_dispute"}
    if action not in allowed:
        flash("Unsupported moderation action.", "error")
        return redirect(url_for("admin_moderator"))
    if action == "delete":
        ok, message = _trash_item(kind, item_id_raw, "ADMIN", "ADMIN")
        flash(message, "success" if ok else "error")
        save_app_state()
        return redirect(url_for("admin_moderator"))
    if kind == "claim":
        claim = next((c for c in business_claims if str(c.get("id")) == item_id_raw), None)
        if not claim:
            flash("Ownership claim not found.", "error")
        elif action in {"approve", "reject"}:
            business = businesses.get(claim.get("business_id"))
            if action == "approve" and business:
                if claim.get("role") == "Owner" or claim.get("claim_type") == "INITIAL_OWNER":
                    current_owner = business.get("primary_owner_id", business.get("submitted_by"))
                    if current_owner and str(current_owner) != str(claim.get("user_id")) and not business.get("ownership_dispute"):
                        claim["status"] = "REJECTED"
                        claim["reviewed_at"] = _business_now()
                        flash("A primary owner is already assigned. Start a formal ownership transfer or dispute instead.", "error")
                    else:
                        business["primary_owner_id"] = claim.get("user_id")
                        business["submitted_by"] = claim.get("user_id")
                        claim["status"] = "APPROVED"
                        claim["reviewed_at"] = _business_now()
                        if business.get("status") in {"PENDING", "PENDING REVIEW", "PENDING_REVIEW"}:
                            business["status"] = "PENDING REVIEW"
                        notify_business_user(claim.get("user_id"), "Business ownership verified", f"Your ownership claim for {business.get('name')} was approved.", business.get("id"))
                        record_business_audit("OWNERSHIP_CLAIM_APPROVED", business.get("id"), "ADMIN", f"Claim {claim.get('id')} approved")
                        flash("Ownership claim approved. Listing publication status remains separate.", "success")
                else:
                    managers = business.setdefault("manager_ids", [])
                    if str(claim.get("user_id")) not in {str(x) for x in managers}:
                        managers.append(claim.get("user_id"))
                    claim["status"] = "APPROVED"; claim["reviewed_at"] = _business_now()
                    notify_business_user(claim.get("user_id"), "Business manager access approved", f"Your manager access for {business.get('name')} was approved.", business.get("id"))
                    record_business_audit("MANAGER_ADDED", business.get("id"), "ADMIN", f"Claim {claim.get('id')} approved")
                    flash("Manager access approved.", "success")
            else:
                claim["status"] = "REJECTED"; claim["reviewed_at"] = _business_now()
                notify_business_user(claim.get("user_id"), "Business claim rejected", f"Your claim for {claim.get('business_name', 'a business')} was rejected. You may submit new evidence if you believe this was an error.", claim.get("business_id"))
                record_business_audit("OWNERSHIP_CLAIM_REJECTED", claim.get("business_id"), "ADMIN", f"Claim {claim.get('id')} rejected")
                flash("Ownership claim rejected and applicant notified.", "success")
    elif kind == "product":
        item = get_marketplace_product(item_id)
        if item is None:
            flash("Marketplace listing not found.", "error")
        elif action in {"approve", "restore"}:
            item["status"] = "APPROVED"; item["moderated_at"] = datetime.now(timezone.utc).isoformat()
            notify_user_action(item.get("user_id"), "Marketplace product published", f"Your product {item.get('title', 'listing')} has been approved and is now visible on the Marketplace.", "marketplace_product", item.get("id"))
            flash("Marketplace listing published/restored. Seller notified.", "success")
        elif action in {"reject", "hide"}:
            item["status"] = "HIDDEN" if action == "hide" else "REJECTED"; item["moderated_at"] = datetime.now(timezone.utc).isoformat()
            notify_user_action(item.get("user_id"), "Marketplace listing needs attention" if action == "hide" else "Marketplace product not approved", f"Your product {item.get('title', 'listing')} was {'temporarily hidden' if action == 'hide' else 'not approved'} by moderation. Please check your account or contact support for details.", "marketplace_product", item.get("id"))
            flash("Marketplace listing hidden/rejected. Seller notified.", "success")
        elif action == "delete":
            marketplace_products.remove(item); flash("Marketplace listing permanently deleted.", "success")
    elif kind == "event":
        event = next((e for e in event_submissions if str(e.get("id", "")) == item_id_raw), None)
        if event is None:
            flash("Event submission not found.", "error")
        elif action == "approve":
            if event_is_expired(event):
                event["status"] = "EXPIRED"; flash("This event date has passed, so it cannot be published.", "error")
            else:
                event["status"] = "APPROVED"; event["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
                notify_user_action(event.get("user_id"), "Event approved and published", f"Your event {event.get('title', 'event')} has been approved and is now published on THE ECOSYSTEM.", "event", event.get("id"))
                flash("Event approved and published. Submitter notified.", "success")
        elif action == "reject":
            event["status"] = "REJECTED"; event["reviewed_at"] = datetime.now().strftime("%b %d, %Y")
            notify_user_action(event.get("user_id"), "Event submission not approved", f"Your event {event.get('title', 'event')} was not approved for publication. Please review the details and contact support if you need clarification.", "event", event.get("id"))
            flash("Event rejected. Submitter notified.", "success")
        elif action == "hide":
            event["status"] = "HIDDEN"; event["hidden_at"] = datetime.now().strftime("%b %d, %Y")
            notify_user_action(event.get("user_id"), "Event temporarily hidden", f"Your event {event.get('title', 'event')} has been temporarily hidden by moderation.", "event", event.get("id"))
            flash("Event hidden from public listings. Submitter notified.", "success")
        elif action == "restore":
            if event_is_expired(event):
                event["status"] = "EXPIRED"; flash("This event has expired and cannot be restored.", "error")
            else:
                event["status"] = "APPROVED"; event["restored_at"] = datetime.now().strftime("%b %d, %Y")
                notify_user_action(event.get("user_id"), "Event restored", f"Your event {event.get('title', 'event')} has been restored to public listings.", "event", event.get("id"))
                flash("Event restored to public listings. Submitter notified.", "success")
        elif action == "delete":
            if event.get("flyer_path"):
                flyer_full_path = os.path.join(PROJECT_FOLDER, "static", event["flyer_path"])
                try:
                    if os.path.isfile(flyer_full_path): os.remove(flyer_full_path)
                except OSError: app.logger.warning("Could not remove event flyer: %s", flyer_full_path)
            event_submissions.remove(event); flash("Event permanently deleted.", "success")
    elif kind == "spotlight":
        campaign = next((c for c in spotlight_campaigns if str(c.get("id")) == str(item_id)), None)
        if campaign is None: flash("Spotlight campaign not found.", "error")
        elif action == "approve":
            if campaign.get("payment_status") != "SUCCESS" or campaign.get("status") != "PENDING_REVIEW":
                flash("Approval blocked: campaign must be pending review with verified successful payment.", "error")
            else:
                plan = SPOTLIGHT_PLANS.get(campaign.get("plan_id"), SPOTLIGHT_PLANS["starter"]); now = datetime.now(timezone.utc)
                campaign.update({"status":"APPROVED", "approved_at":now.isoformat(), "reviewed_at":now.isoformat(), "starts_at":now.isoformat(), "expires_at":(now+timedelta(days=plan["duration_days"])).isoformat()})
                global marketplace_premium_ad
                marketplace_premium_ad = {"active":True,"campaign_id":campaign["id"],"company":campaign.get("company",""),"headline":campaign.get("headline",""),"description":campaign.get("description",""),"button_text":campaign.get("button_text","Explore offer"),"target_url":campaign.get("target_url",""),"image_url":campaign.get("image_url",""),"expires_at":campaign["expires_at"],"plan_id":campaign.get("plan_id")}
                notify_user_action(campaign.get("user_id"), "Spotlight campaign published", f"Your paid Spotlight campaign for {campaign.get('company', 'your business')} has been approved and is now live.", "spotlight", campaign.get("id"))
                flash("Paid Spotlight campaign approved and published. Customer notified.", "success")
        elif action == "reject":
            if campaign.get("status") == "APPROVED" and marketplace_premium_ad.get("campaign_id") == campaign.get("id"):
                marketplace_premium_ad = {}
            campaign.update({"status":"REJECTED", "reviewed_at":datetime.now(timezone.utc).isoformat()})
            notify_user_action(campaign.get("user_id"), "Spotlight campaign not approved", f"Your Spotlight campaign for {campaign.get('company', 'your business')} was not approved. Your payment record is retained for support/refund handling.", "spotlight", campaign.get("id"))
            flash("Spotlight campaign rejected; payment remains recorded for support/refund handling. Customer notified.", "success")
        elif action in {"hide", "restore"}:
            flash("Use approve/reject for Spotlight campaign publishing decisions.", "error")
        elif action == "delete":
            if marketplace_premium_ad.get("campaign_id") == campaign.get("id"): marketplace_premium_ad = {}
            spotlight_campaigns.remove(campaign); flash("Spotlight campaign record deleted.", "success")
    elif kind == "premium_listing":
        order = _premium_listing_order(item_id_raw)
        if order is None:
            flash("Premium listing application not found.", "error")
        elif action == "approve":
            if order.get("payment_status") != "SUCCESS":
                flash("Cannot approve: verified successful payment is required.", "error")
            elif order.get("campaign_type") == "multi_item":
                now = datetime.now(timezone.utc)
                starts_at = now.isoformat()
                expires_at = (now + timedelta(days=30 * int(order.get("duration_months", 1)))).isoformat()
                missing = []
                for campaign_item in order.get("campaign_items", []):
                    if campaign_item.get("kind") == "business":
                        target = businesses.get(int(campaign_item.get("id"))) if str(campaign_item.get("id", "")).isdigit() else None
                    else:
                        target = next((e for e in event_submissions if str(e.get("id")) == str(campaign_item.get("id"))), None)
                    if target is None:
                        missing.append(campaign_item.get("name", "listing")); continue
                    covered_states = list(dict.fromkeys([campaign_item.get("primary_state", "")] + campaign_item.get("additional_states", [])))
                    existing_states = target.get("premium_states") or []
                    target["premium_states"] = list(dict.fromkeys(existing_states + covered_states))
                    target["premium_status"] = "ACTIVE"
                    if campaign_item.get("kind") == "business":
                        target["premium_listing"] = True
                    target["premium_campaign_order_id"] = order["id"]
                    target["premium_duration_months"] = order.get("duration_months", 1)
                    target["premium_starts_at"] = starts_at
                    target["premium_expires_at"] = expires_at
                if missing:
                    order.update({"status": "APPROVAL_BLOCKED", "approval_error": "Missing listings: " + ", ".join(missing)})
                    flash("Approval blocked because one or more campaign listings no longer exist: " + ", ".join(missing), "error")
                else:
                    order.update({"status": "ACTIVE", "approved_at": starts_at, "starts_at": starts_at, "expires_at": expires_at})
                    notify_user_action(order.get("user_id"), "Premium campaign approved and published", f"Your payment has been verified and your premium campaign has been approved. Your selected listings are now active across the approved states until {expires_at[:10]}.", "premium_listing", order.get("id"))
                    flash("Premium campaign approved and activated for all selected listings. Customer notified.", "success")
            else:
                business = businesses.get(order.get("business_id"))
                if not business:
                    flash("Cannot approve: a valid business record is required.", "error")
                else:
                    now = datetime.now(timezone.utc)
                    order.update({"status": "ACTIVE", "approved_at": now.isoformat(), "starts_at": now.isoformat(),
                                  "expires_at": (now + timedelta(days=30 * int(order.get("duration_months", 1)))).isoformat()})
                    business.update({"status": "APPROVED", "premium_status": "ACTIVE", "premium_starts_at": order["starts_at"],
                                     "premium_expires_at": order["expires_at"]})
                    submission = next((x for x in business_submissions if str(x.get("id")) == str(order.get("submission_id"))), None)
                    if submission: submission["status"] = "APPROVED"
                    notify_user_action(order.get("user_id"), "Premium listing approved and published", f"Your payment has been verified and {business.get('name', 'your business')} has been approved for premium publishing. Your campaign is now active until {order['expires_at'][:10]}.", "premium_listing", order.get("id"))
                    flash("Premium business listing approved and subscription activated. Customer notified.", "success")
        elif action == "reject":
            order.update({"status": "REJECTED", "reviewed_at": datetime.now(timezone.utc).isoformat()})
            if order.get("campaign_type") == "multi_item":
                for campaign_item in order.get("campaign_items", []):
                    if campaign_item.get("kind") == "business":
                        target = businesses.get(int(campaign_item.get("id"))) if str(campaign_item.get("id", "")).isdigit() else None
                    else:
                        target = next((e for e in event_submissions if str(e.get("id")) == str(campaign_item.get("id"))), None)
                    if target and target.get("premium_campaign_order_id") == order.get("id"):
                        target["premium_status"] = "REJECTED"
                notify_user_action(order.get("user_id"), "Premium campaign not approved", "Your premium campaign was not approved by moderation. Existing organic listings remain available. Please contact support if you need clarification about the decision.", "premium_listing", order.get("id"))
                flash("Premium campaign rejected. Existing organic listings remain available. Customer notified.", "success")
            else:
                business = businesses.get(order.get("business_id"))
                if business:
                    # Reject the paid promotion, not an already-approved organic profile.
                    business["premium_status"] = "REJECTED"
                    if str(business.get("status", "")).upper() in {"PENDING REVIEW", "PENDING", "PENDING_REVIEW"} and order.get("submission_id"):
                        business["status"] = "REJECTED"
                submission = next((x for x in business_submissions if str(x.get("id")) == str(order.get("submission_id"))), None)
                if submission: submission["status"] = "REJECTED"
                notify_user_action(order.get("user_id"), "Premium listing not approved", f"Your premium listing request for {order.get('business_name', 'your business')} was not approved. Please contact support if you need clarification.", "premium_listing", order.get("id"))
                flash("Premium business listing rejected. Customer notified.", "success")
        elif action == "delete":
            premium_listing_orders[:] = [x for x in premium_listing_orders if str(x.get("id")) != item_id_raw]
            flash("Premium listing application record deleted.", "success")
        else:
            flash("Unsupported premium listing action.", "error")
    elif kind == "report":
        item = next((r for r in reports if str(r.get("id", "")) == item_id_raw), None)
        if item is None: flash("Report not found.", "error")
        elif action in {"resolve", "approve"}: item["status"] = "REVIEWED"; flash("Report marked reviewed.", "success")
        elif action == "delete": reports.remove(item); flash("Report removed.", "success")
        else: flash("Use resolve or delete for reports.", "error")
    elif kind == "business":
        business = businesses.get(item_id_int) if item_id_int is not None else None
        if business is None:
            flash("Business not found.", "error")
        elif action == "approve":
            submissions = [s for s in business_submissions if str(s.get("business_id")) == str(item_id_int)]
            if any(s.get("premium_listing_order_id") and (_premium_listing_order(s.get("premium_listing_order_id")) or {}).get("payment_status") != "SUCCESS" for s in submissions):
                flash("Cannot approve a paid premium listing before payment verification.", "error")
            else:
                business["status"] = "APPROVED"
                for submission in submissions: submission["status"] = "APPROVED"
                record_business_audit("BUSINESS_LISTING_APPROVED", item_id_int, "ADMIN", "Listing approved")
                notify_business_user(business.get("primary_owner_id", business.get("submitted_by")), "Business listing approved", f"{business.get('name')} is approved for public discovery, subject to moderation and archive status.", item_id_int)
                flash("Business listing approved. Ownership status remains separately tracked.", "success")
        elif action == "reject":
            business["status"] = "REJECTED"
            for submission in business_submissions:
                if str(submission.get("business_id")) == str(item_id_int): submission["status"] = "REJECTED"
            record_business_audit("BUSINESS_LISTING_REJECTED", item_id_int, "ADMIN", "Listing rejected")
            notify_business_user(business.get("primary_owner_id", business.get("submitted_by")), "Business listing rejected", f"{business.get('name')} was not approved for public discovery. Please review the submission requirements and contact support if needed.", item_id_int)
            flash("Business listing rejected and submitter notified.", "success")
        elif action == "hide":
            if business.get("status") != "HIDDEN": business["_admin_previous_status"] = business.get("status", "APPROVED"); business["status"] = "HIDDEN"
            record_business_audit("BUSINESS_HIDDEN", item_id_int, "ADMIN", "Hidden from discovery")
            notify_business_user(business.get("primary_owner_id", business.get("submitted_by")), "Business listing hidden", f"{business.get('name')} has been hidden by moderation. Check your account for details.", item_id_int)
            flash("Business hidden from public discovery.", "success")
        elif action == "restore":
            business["archived"] = False
            business["status"] = business.pop("_admin_previous_status", None) or ("APPROVED" if all(str(s.get("status")).upper() == "APPROVED" for s in business_submissions if str(s.get("business_id")) == str(item_id_int)) else "PENDING REVIEW")
            record_business_audit("BUSINESS_RESTORED_BY_ADMIN", item_id_int, "ADMIN", "Admin restored business listing")
            notify_business_user(business.get("primary_owner_id", business.get("submitted_by")), "Business listing restored", f"{business.get('name')} has been restored by an administrator.", item_id_int)
            flash("Business restored subject to its listing approval state.", "success")
        elif action == "archive":
            if str(business.get("primary_owner_id", business.get("submitted_by", ""))) != str(session.get("user_id", "")) and not session.get("admin_logged_in"):
                flash("Only the primary owner or admin can archive this business.", "error")
            else:
                business["archived"] = True; business["status"] = "ARCHIVED"
                record_business_audit("BUSINESS_ARCHIVED", item_id_int, "ADMIN", "Archived from public discovery")
                notify_business_user(business.get("primary_owner_id", business.get("submitted_by")), "Business archived", f"{business.get('name')} has been archived and removed from public discovery. Records are preserved.", item_id_int)
                flash("Business archived. Records and reviews were preserved.", "success")
        elif action == "clear_dispute":
            business["ownership_dispute"] = False
            business["status"] = "APPROVED" if business.get("status") == "DISPUTED" else business.get("status", "APPROVED")
            record_business_audit("OWNERSHIP_DISPUTE_CLEARED", item_id_int, "ADMIN", "Admin resolved ownership dispute")
            flash("Ownership dispute cleared by admin.", "success")
        elif action == "delete":
            removed_review_ids = {r.get("id") for r in reviews if r.get("business_id") == item_id_int}
            reviews[:] = [r for r in reviews if r.get("business_id") != item_id_int]
            business_claims[:] = [c for c in business_claims if c.get("business_id") != item_id_int]
            business_submissions[:] = [x for x in business_submissions if x.get("business_id") != item_id_int]
            removed_entry_ids = {e.get("id") for e in competition_entries if e.get("business_id") == item_id_int}
            competition_entries[:] = [e for e in competition_entries if e.get("business_id") != item_id_int]
            competition_votes[:] = [v for v in competition_votes if v.get("entry_id") not in removed_entry_ids]
            reports[:] = [r for r in reports if not ((r.get("target_type") == "business" and str(r.get("target_id")) == str(item_id_int)) or (r.get("target_type") == "review" and r.get("target_id") in removed_review_ids))]
            del businesses[item_id_int]; flash("Business and associated reviews permanently deleted.", "success")
    elif kind == "review":
        review = next((r for r in reviews if str(r.get("id", "")) == item_id_raw), None)
        if action == "delete" and review:
            reviews.remove(review); reports[:] = [r for r in reports if not (r.get("target_type") == "review" and str(r.get("target_id")) == str(item_id_raw))]; flash("Review/comment permanently deleted.", "success")
        else: flash("Review not found or unsupported action.", "error")
    else:
        flash("Unknown moderation item.", "error")
    save_app_state()
    return redirect(url_for("admin_moderator"))


# =========================
# ADMIN — BUSINESS / REVIEW MODERATION
# =========================

@app.route("/admin/moderation")
def admin_moderation():
    if not admin_required():
        return redirect(url_for("admin_login"))

    business_items = []
    for business in sorted(businesses.values(), key=lambda item: item.get("id", 0)):
        business_items.append({
            "business": business,
            "review_count": len(get_business_reviews(business["id"])),
            "report_count": sum(
                1 for report in reports
                if report.get("target_type") == "business"
                and str(report.get("target_id")) == str(business["id"])
                and report.get("status", "OPEN") == "OPEN"
            )
        })

    review_items = []
    for review in sorted(reviews, key=lambda item: item.get("id", 0), reverse=True):
        review_items.append({
            "review": review,
            "business": businesses.get(review.get("business_id")),
            "report_count": sum(
                1 for report in reports
                if report.get("target_type") == "review"
                and str(report.get("target_id")) == str(review.get("id"))
                and report.get("status", "OPEN") == "OPEN"
            )
        })

    open_reports = [
        report for report in reports
        if report.get("status", "OPEN") == "OPEN"
    ]
    open_reports.sort(key=lambda item: item.get("created_at", ""), reverse=True)

    page = r"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Moderation — THE ECOSYSTEM</title>
      <style>
        :root{color-scheme:dark}
        *{box-sizing:border-box}
        body{margin:0;background:#080808;color:#f5f5f5;font:15px/1.5 Arial,sans-serif}
        header{padding:22px clamp(16px,4vw,48px);background:#111;border-bottom:1px solid #292929;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
        header a,.link{color:#ff5353;text-decoration:none}
        main{max-width:1200px;margin:auto;padding:26px clamp(14px,3vw,32px) 60px}
        h1{margin:0;font-size:clamp(24px,4vw,36px)} h2{margin-top:32px}
        .muted{color:#aaa}.notice{padding:12px 14px;border-radius:10px;background:#171717;margin:12px 0}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:14px}
        .item{background:#111;border:1px solid #2b2b2b;border-radius:14px;padding:17px;min-width:0}
        .item h3{margin:0 0 8px;font-size:18px}.pill{display:inline-block;font-size:12px;border:1px solid #555;border-radius:999px;padding:3px 9px;margin:2px 4px 8px 0;color:#ddd}
        .pill.warn{border-color:#b33;color:#ff7777}.bodytext{white-space:pre-wrap;overflow-wrap:anywhere;color:#ddd}
        form{display:inline-block;margin:5px 5px 0 0}button{border:0;border-radius:8px;padding:10px 12px;font-weight:700;cursor:pointer}
        .danger{background:#c62828;color:#fff}.secondary{background:#292929;color:#fff}.good{background:#176b42;color:#fff}
        .toplinks{display:flex;gap:14px;flex-wrap:wrap}.empty{border:1px dashed #444;padding:18px;border-radius:12px;color:#aaa}
        @media(max-width:520px){header{align-items:flex-start}button{padding:9px;font-size:12px}}
      </style>
    </head>
    <body>
      <header>
        <div><div class="muted">THE ECOSYSTEM · ADMIN</div><h1>Content moderation</h1></div>
        <nav class="toplinks"><a href="{{ url_for('admin_dashboard') }}">Dashboard</a><a href="{{ url_for('admin_logout') }}">Log out</a></nav>
      </header>
      <main>
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% for category, message in messages %}<div class="notice">{{ message }}</div>{% endfor %}
        {% endwith %}
        <p class="muted">Review reports, temporarily hide suspicious listings, restore listings after review, or permanently remove fraudulent businesses and abusive reviews.</p>

        <h2>Open reports ({{ open_reports|length }})</h2>
        {% if open_reports %}
        <div class="grid">
          {% for report in open_reports %}
          <article class="item">
            <span class="pill warn">{{ report.target_type|upper }} REPORT</span>
            <span class="pill">#{{ report.id }}</span>
            <p><strong>Reason:</strong> {{ report.reason or 'No reason provided' }}</p>
            <p class="muted">Submitted by {{ report.reporter_name or 'Community member' }} · {{ report.created_at }}</p>
            {% if report.target_type == 'business' %}
              <p>Business: {{ businesses.get(report.target_id|int, {}).get('name', 'Business no longer available') }}</p>
            {% elif report.target_type == 'review' %}
              {% set reported_review = namespace(item=None) %}
              {% for r in review_items %}{% if r.review.id|string == report.target_id|string %}{% set reported_review.item = r %}{% endif %}{% endfor %}
              {% if reported_review.item %}
                <p>Business: {{ reported_review.item.business.name if reported_review.item.business else 'Business removed' }}</p>
                <p class="bodytext">{{ reported_review.item.review.review }}</p>
              {% else %}<p class="muted">The reported review is no longer available.</p>{% endif %}
            {% endif %}
            <form method="post" action="{{ url_for('admin_resolve_report', report_id=report.id) }}" onsubmit="return confirm('Mark this report as reviewed?')"><button class="good">Mark reviewed</button></form>
            {% if report.target_type == 'business' and businesses.get(report.target_id|int) %}
              <form method="post" action="{{ url_for('admin_hide_business', business_id=report.target_id|int) }}" onsubmit="return confirm('Temporarily hide this business from public discovery?')"><button class="secondary">Hide business</button></form>
              <form method="post" action="{{ url_for('admin_delete_business', business_id=report.target_id|int) }}" onsubmit="return confirm('PERMANENTLY delete this business and its associated reviews? This cannot be undone.')"><button class="danger">Delete business</button></form>
            {% elif report.target_type == 'review' %}
              <form method="post" action="{{ url_for('admin_delete_review', review_id=report.target_id|int) }}" onsubmit="return confirm('Permanently delete this review?')"><button class="danger">Delete review</button></form>
            {% endif %}
          </article>
          {% endfor %}
        </div>
        {% else %}<div class="empty">No open reports right now.</div>{% endif %}

        <h2>Businesses ({{ business_items|length }})</h2>
        <div class="grid">
          {% for item in business_items %}
          {% set business = item.business %}
          <article class="item">
            <h3>{{ business.name }}</h3>
            <span class="pill">{{ business.category or 'UNCATEGORIZED' }}</span>
            <span class="pill">{{ business.status or 'APPROVED' }}</span>
            {% if item.report_count %}<span class="pill warn">{{ item.report_count }} open report(s)</span>{% endif %}
            <p class="muted">{{ business.location or business.city or 'Location not specified' }}</p>
            <p class="bodytext">{{ business.description or business.about or '' }}</p>
            <p class="muted">{{ item.review_count }} review(s)</p>
            <p>
              {% if business.status == 'HIDDEN' %}
                <form method="post" action="{{ url_for('admin_restore_business', business_id=business.id) }}" onsubmit="return confirm('Restore this business to its previous status?')"><button class="good">Restore business</button></form>
              {% else %}
                <form method="post" action="{{ url_for('admin_hide_business', business_id=business.id) }}" onsubmit="return confirm('Temporarily hide this business from public discovery?')"><button class="secondary">Hide business</button></form>
              {% endif %}
              <form method="post" action="{{ url_for('admin_delete_business', business_id=business.id) }}" onsubmit="return confirm('PERMANENTLY delete {{ business.name|e }} and its associated reviews? This cannot be undone.')"><button class="danger">Delete business</button></form>
            </p>
          </article>
          {% else %}<div class="empty">No businesses found.</div>{% endfor %}
        </div>

        <h2>Reviews and comments ({{ review_items|length }})</h2>
        <div class="grid">
          {% for item in review_items %}
          {% set review = item.review %}
          <article class="item">
            <h3>{{ review.user_name or 'Community member' }} · {{ review.rating }}/5 ★</h3>
            <p class="muted">{{ item.business.name if item.business else 'Business removed' }} · {{ review.created_at or '' }}</p>
            {% if item.report_count %}<span class="pill warn">{{ item.report_count }} open report(s)</span>{% endif %}
            <p class="bodytext">{{ review.review }}</p>
            <form method="post" action="{{ url_for('admin_delete_review', review_id=review.id) }}" onsubmit="return confirm('Permanently delete this review/comment?')"><button class="danger">Delete review</button></form>
          </article>
          {% else %}<div class="empty">No reviews or comments have been posted.</div>{% endfor %}
        </div>
      </main>
    </body>
    </html>
    """
    return render_template_string(
        page,
        business_items=business_items,
        review_items=review_items,
        open_reports=open_reports,
        businesses=businesses
    )


@app.route("/business/<int:business_id>/report", methods=["POST"])
def report_business(business_id):
    if business_id not in businesses:
        return "Business not found", 404
    if session.get("user_id") is None:
        flash("Please log in to report a business.", "error")
        return redirect(url_for("login"))

    reason = request.form.get("reason", "").strip()[:500]
    if not reason:
        flash("Please provide a reason for reporting this business.", "error")
        return redirect(url_for("business_profile", business_id=business_id))

    reporter = get_user_by_id(session.get("user_id"))
    reports.append({
        "id": max((item.get("id", 0) for item in reports), default=0) + 1,
        "target_type": "business",
        "target_id": business_id,
        "reporter_id": session.get("user_id"),
        "reporter_name": reporter.get("name", "Community member") if reporter else "Community member",
        "reason": reason,
        "status": "OPEN",
        "created_at": datetime.now().strftime("%b %d, %Y %H:%M")
    })
    notify_admin("Business report received", f"{reporter.get('name', 'A community member') if reporter else 'A community member'} reported {businesses[business_id].get('name')}: {reason}", "report", reports[-1].get("id"))
    save_app_state()
    flash("Your report has been sent to the admin team for review.", "success")
    return redirect(url_for("business_profile", business_id=business_id))


@app.route("/review/<int:review_id>/report", methods=["POST"])
def report_review(review_id):
    review = next((item for item in reviews if item.get("id") == review_id), None)
    if review is None:
        return "Review not found", 404
    if session.get("user_id") is None:
        flash("Please log in to report a review.", "error")
        return redirect(url_for("login"))

    reason = request.form.get("reason", "").strip()[:500]
    if not reason:
        flash("Please provide a reason for reporting this review.", "error")
        return redirect(url_for("business_profile", business_id=review["business_id"]))

    reporter = get_user_by_id(session.get("user_id"))
    reports.append({
        "id": max((item.get("id", 0) for item in reports), default=0) + 1,
        "target_type": "review",
        "target_id": review_id,
        "reporter_id": session.get("user_id"),
        "reporter_name": reporter.get("name", "Community member") if reporter else "Community member",
        "reason": reason,
        "status": "OPEN",
        "created_at": datetime.now().strftime("%b %d, %Y %H:%M")
    })
    notify_admin("Review report received", f"{reporter.get('name', 'A community member') if reporter else 'A community member'} reported review #{review_id}: {reason}", "report", reports[-1].get("id"))
    save_app_state()
    flash("Your report has been sent to the admin team for review.", "success")
    return redirect(url_for("business_profile", business_id=review["business_id"]))


@app.route("/admin/business/<int:business_id>/hide", methods=["POST"])
def admin_hide_business(business_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    business = businesses.get(business_id)
    if business is None:
        flash("Business not found.", "error")
        return redirect(url_for("admin_moderation"))

    if business.get("status") != "HIDDEN":
        business["_admin_previous_status"] = business.get("status", "APPROVED")
        business["status"] = "HIDDEN"
    flash(f"{business.get('name', 'Business')} is now hidden from public discovery.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/business/<int:business_id>/restore", methods=["POST"])
def admin_restore_business(business_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    business = businesses.get(business_id)
    if business is None:
        flash("Business not found.", "error")
        return redirect(url_for("admin_moderation"))

    previous_status = business.pop("_admin_previous_status", None)
    business["status"] = previous_status or "APPROVED"
    flash(f"{business.get('name', 'Business')} has been restored.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/business/<int:business_id>/delete", methods=["POST"])
def admin_delete_business(business_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    business = businesses.get(business_id)
    if business is None:
        flash("Business not found or already deleted.", "error")
        return redirect(url_for("admin_moderation"))

    # Remove uploaded image files belonging to this business, but only files
    # contained inside the configured business-upload directory.
    upload_root = os.path.abspath(app.config["UPLOAD_FOLDER"])
    for photo in business.get("photos", []):
        photo_path = photo.get("url") if isinstance(photo, dict) else photo
        if not isinstance(photo_path, str):
            continue
        normalized = photo_path.replace("\\", "/")
        marker = "/static/uploads/businesses/"
        if marker in normalized:
            relative_name = normalized.split(marker, 1)[1]
            candidate = os.path.abspath(os.path.join(upload_root, relative_name))
            if candidate.startswith(upload_root + os.sep) and os.path.isfile(candidate):
                try:
                    os.remove(candidate)
                except OSError:
                    app.logger.warning("Could not remove business photo: %s", candidate)

    removed_review_ids = {
        item.get("id") for item in reviews
        if item.get("business_id") == business_id
    }
    reviews[:] = [item for item in reviews if item.get("business_id") != business_id]
    business_claims[:] = [item for item in business_claims if item.get("business_id") != business_id]
    business_submissions[:] = [item for item in business_submissions if item.get("business_id") != business_id]

    removed_entry_ids = {
        item.get("id") for item in competition_entries
        if item.get("business_id") == business_id
    }
    competition_entries[:] = [
        item for item in competition_entries
        if item.get("business_id") != business_id
    ]
    competition_votes[:] = [
        item for item in competition_votes
        if item.get("entry_id") not in removed_entry_ids
    ]
    reports[:] = [
        item for item in reports
        if not (
            (item.get("target_type") == "business" and str(item.get("target_id")) == str(business_id))
            or (item.get("target_type") == "review" and item.get("target_id") in removed_review_ids)
        )
    ]
    del businesses[business_id]
    flash("Business and its associated reviews have been permanently deleted.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/review/<int:review_id>/delete", methods=["POST"])
def admin_delete_review(review_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    review = next((item for item in reviews if item.get("id") == review_id), None)
    if review is None:
        flash("Review not found or already deleted.", "error")
        return redirect(url_for("admin_moderation"))

    reviews.remove(review)
    reports[:] = [
        item for item in reports
        if not (item.get("target_type") == "review" and str(item.get("target_id")) == str(review_id))
    ]
    flash("Review/comment permanently deleted.", "success")
    return redirect(url_for("admin_moderation"))


@app.route("/admin/report/<int:report_id>/resolve", methods=["POST"])
def admin_resolve_report(report_id):
    if not admin_required():
        return redirect(url_for("admin_login"))
    report = next((item for item in reports if item.get("id") == report_id), None)
    if report is None:
        flash("Report not found.", "error")
    else:
        report["status"] = "REVIEWED"
        notify_user_action(report.get("reporter_id"), "Your report has been reviewed", f"The admin team has reviewed your report #{report_id}. Thank you for helping keep THE ECOSYSTEM trustworthy.", "report", report_id)
        flash("Report marked as reviewed. Reporter notified.", "success")
    save_app_state()
    return redirect(url_for("admin_moderation"))


# =========================
# ADMIN — LOGOUT
# =========================

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


# =========================
# LOGOUT
# =========================

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("home"))



# =========================
# DATABASE STORAGE (PostgreSQL on Render, SQLite locally)
# =========================

PROJECT_FOLDER = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(PROJECT_FOLDER, "ecosystem.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


def _normalise_database_url(url):
    # Render may provide postgres:// while psycopg2 expects postgresql://.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


class _PostgresConnection:
    """Small compatibility adapter for the app's existing SQLite-style calls."""
    def __init__(self, raw_connection):
        self.raw = raw_connection
        self.row_factory = None  # Existing code assigns sqlite3.Row; DictCursor supports key and index access.

    def execute(self, sql, params=None):
        # Existing app SQL uses SQLite qmark placeholders. Convert them for psycopg2.
        sql = sql.replace("?", "%s")
        cursor = self.raw.cursor()
        cursor.execute(sql, params or ())
        return cursor

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()
        return False


def connect_database():
    """Use Render PostgreSQL when DATABASE_URL is configured; otherwise local SQLite."""
    if DATABASE_URL:
        if psycopg2 is None:
            raise RuntimeError("PostgreSQL support is missing. Install psycopg2-binary from requirements.txt.")
        raw = psycopg2.connect(_normalise_database_url(DATABASE_URL), cursor_factory=DictCursor, connect_timeout=15)
        return _PostgresConnection(raw)

    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_database():
    if not DATABASE_URL:
        os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    with connect_database() as connection:
        if not DATABASE_URL:
            # WAL is SQLite-only.
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_password_reset_user_id "
            "ON password_reset_tokens(user_id)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_verification_user_id "
            "ON email_verification_tokens(user_id)"
        )
        connection.commit()


def save_app_state():
    state = {
        "users": users,
        "business_claims": business_claims,
        "business_submissions": business_submissions,
        "business_audit_log": business_audit_log,
        "admin_activity_log": admin_activity_log,
        "business_notifications": business_notifications,
        "event_submissions": event_submissions,
        "reviews": reviews,
        "competition_entries": competition_entries,
        "competition_votes": competition_votes,
        "reports": reports,
        "showcase_activity": showcase_activity,
        "showcase_posts": showcase_posts,
        "businesses": businesses,
        "competitions_data": competitions_data,
        "marketplace_products": marketplace_products,
        "marketplace_premium_ad": marketplace_premium_ad,
        "spotlight_payments": spotlight_payments,
        "spotlight_campaigns": spotlight_campaigns,
        "premium_listing_orders": premium_listing_orders,
        "deleted_items": deleted_items
    }

    serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    with connect_database() as connection:
        connection.execute("""
            INSERT INTO app_state (id, data)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data
        """, (serialized,))
        connection.commit()

    global HEALTH_LAST_SAVE_AT, HEALTH_LAST_SAVE_ERROR
    HEALTH_LAST_SAVE_AT = datetime.now(timezone.utc).isoformat()
    HEALTH_LAST_SAVE_ERROR = ""


def load_app_state():
    global users, business_claims, business_submissions, business_audit_log, admin_activity_log, business_notifications, event_submissions
    global reviews, competition_entries, competition_votes, reports, showcase_activity, showcase_posts
    global businesses, competitions_data, marketplace_products, marketplace_premium_ad, spotlight_payments, spotlight_campaigns, premium_listing_orders, deleted_items

    with connect_database() as connection:
        row = connection.execute(
            "SELECT data FROM app_state WHERE id = 1"
        ).fetchone()

    if row is None:
        save_app_state()
        return

    try:
        state = json.loads(row[0])
        if not isinstance(state, dict):
            raise ValueError("Stored app state must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        # Do not silently replace a damaged database with empty data.
        app.logger.exception("The saved app_state data could not be read: %s", exc)
        raise RuntimeError(
            "ecosystem.db contains unreadable saved application data. Back up the database before repairing it."
        ) from exc

    users = state.get("users", [])
    # Temporary bypass: mark all stored accounts login-ready, including accounts
    # created previously that were waiting for an email verification link.
    for saved_user in users:
        saved_user["email_verified"] = True
    business_claims = state.get("business_claims", [])
    business_submissions = state.get("business_submissions", [])
    business_audit_log = state.get("business_audit_log", [])
    admin_activity_log = state.get("admin_activity_log", [])
    business_notifications = state.get("business_notifications", [])
    event_submissions = state.get("event_submissions", [])
    reviews = state.get("reviews", [])
    competition_entries = state.get("competition_entries", [])
    competition_votes = state.get("competition_votes", [])
    reports = state.get("reports", [])
    showcase_activity = state.get("showcase_activity", [])
    showcase_posts = state.get("showcase_posts", [])

    # Migrate legacy nominee records to the new 15-category structure by their
    # saved title first, so existing nominations do not silently move to a new award.
    def _competition_id_for_legacy_entry(entry):
        title = normalize_location(entry.get("competition_title", ""))
        title_rules = [
            ("restaurant", 1), ("food spot", 1),
            ("hotel", 2), ("hospitality", 2),
            ("fashion", 3), ("clothing brand", 3),
            ("barber", 4), ("salon", 4), ("beauty", 4),
            ("supermarket", 5), ("retail store", 5),
            ("real estate", 6), ("property company", 6),
            ("healthcare", 7), ("pharmacy", 7),
            ("school", 8), ("educational institution", 8),
            ("transport", 9), ("logistics", 9),
            ("technology startup", 10), ("local startup", 10), ("innovation", 10),
            ("agriculture", 11), ("manufacturing", 11),
            ("events", 12), ("lounge", 12), ("entertainment venue", 12),
            ("home services", 13), ("skilled professional", 13),
            ("content creator", 14), ("media personality", 14), ("best creator", 14),
            ("people's choice", 15), ("peoples choice", 15),
        ]
        for marker, new_id in title_rules:
            if marker in title:
                return new_id
        # Fallback for very old records that did not save a title.
        return {1: 1, 2: 3, 3: 1, 4: 4, 5: 2, 6: 14, 7: 10, 8: 15}.get(entry.get("competition_id"), entry.get("competition_id"))

    entry_by_id = {}
    for saved_entry in competition_entries:
        new_competition_id = _competition_id_for_legacy_entry(saved_entry)
        if new_competition_id in {item.get("id") for item in competitions_data}:
            saved_entry["competition_id"] = new_competition_id
        entry_by_id[saved_entry.get("id")] = saved_entry

    # Follow each migrated nominee's category and prevent duplicate historical
    # votes if two old categories were combined into one new category.
    migrated_votes = []
    seen_user_categories = set()
    for saved_vote in sorted(competition_votes, key=lambda item: item.get("id", 0)):
        saved_entry = entry_by_id.get(saved_vote.get("entry_id"))
        if saved_entry:
            saved_vote["competition_id"] = saved_entry.get("competition_id")
        key = (str(saved_vote.get("user_id")), saved_vote.get("competition_id"))
        if key in seen_user_categories:
            continue
        seen_user_categories.add(key)
        migrated_votes.append(saved_vote)
    competition_votes = migrated_votes

    saved_businesses = state.get("businesses", {})

    for business_id, business in businesses.items():
        saved_businesses.setdefault(str(business_id), business)

    businesses = {
        int(business_id): business
        for business_id, business in saved_businesses.items()
    }

    # Keep configured competition titles/descriptions authoritative over stale
    # values persisted in ecosystem.db. Otherwise an old "Best Restaurant in
    # Ibadan" record can override the updated Abuja title every time Flask starts.
    configured_competitions = {
        item["id"]: item.copy() for item in competitions_data
    }
    saved_competitions = state.get("competitions_data", [])
    if not isinstance(saved_competitions, list):
        saved_competitions = []

    saved_by_id = {
        item.get("id"): item for item in saved_competitions
        if isinstance(item, dict) and item.get("id") is not None
    }

    def _configured_id_for_saved_competition(saved):
        title = normalize_location(saved.get("title", ""))
        rules = [
            (("restaurant", "food spot"), 1),
            (("hotel", "hospitality"), 2),
            (("fashion", "clothing brand"), 3),
            (("barber", "salon", "beauty"), 4),
            (("supermarket", "retail store"), 5),
            (("real estate", "property company"), 6),
            (("healthcare", "pharmacy"), 7),
            (("school", "educational institution"), 8),
            (("transport", "logistics"), 9),
            (("technology", "startup", "innovation"), 10),
            (("agriculture", "manufacturing"), 11),
            (("events", "lounge", "entertainment venue"), 12),
            (("home services", "skilled professional"), 13),
            (("creator", "media personality"), 14),
            (("people's choice", "peoples choice"), 15),
        ]
        for markers, target_id in rules:
            if any(marker in title for marker in markers):
                return target_id
        return {1: 1, 2: 3, 3: 1, 4: 4, 5: 2, 6: 14, 7: 10, 8: 15}.get(saved.get("id"), saved.get("id"))

    saved_status_by_id = {}
    for saved in saved_competitions:
        if not isinstance(saved, dict):
            continue
        target_id = _configured_id_for_saved_competition(saved)
        if target_id not in configured_competitions:
            continue
        current = saved_status_by_id.get(target_id, {})
        # When the old restaurant and food-spot awards merge, retain OPEN if either was open.
        for field in ("status", "voting_status"):
            value = saved.get(field)
            if value is None:
                continue
            if not current.get(field) or str(value).upper() == "OPEN":
                current[field] = value
        saved_status_by_id[target_id] = current

    merged_competitions = []
    for configured_id, configured in configured_competitions.items():
        merged = configured.copy()
        # Keep admin-controlled status/voting state for the matching award while
        # refreshing category copy and labels from the current configuration.
        for field, value in saved_status_by_id.get(configured_id, {}).items():
            merged[field] = value
        merged_competitions.append(merged)
    competitions_data = merged_competitions
    marketplace_products = state.get("marketplace_products", [])
    marketplace_premium_ad = state.get("marketplace_premium_ad", {})
    spotlight_payments = state.get("spotlight_payments", [])
    spotlight_campaigns = state.get("spotlight_campaigns", [])
    premium_listing_orders = state.get("premium_listing_orders", [])
    deleted_items = state.get("deleted_items", [])


init_database()
load_app_state()
# Persist the temporary email-verification bypass for existing pending accounts.
save_app_state()


@app.after_request
def persist_app_state(response):
    global HEALTH_LAST_SAVE_ERROR
    try:
        _cleanup_read_notifications()
        save_app_state()
    except Exception as exc:
        HEALTH_LAST_SAVE_ERROR = f"{type(exc).__name__}: application state save failed"
        app.logger.exception("Could not save application state")

    return response


# =========================
# RUN APPLICATION
# =========================

if __name__ == "__main__":
    app.run(debug=True)
