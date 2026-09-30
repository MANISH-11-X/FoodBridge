
from flask import (
    Flask, request, render_template,
    redirect, url_for, session, flash
)
import markdown
from datetime import datetime
from bson.objectid import ObjectId
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import os

from sklearn.linear_model import LogisticRegression

from google import genai

genai_client = genai.Client(api_key="AQ.Ab8RN6Im0hAKItV1TCJSAgscOuFynaZg_ar5jMiDj2DY9nG8ew")

# --------------------------------------------------
# ML: FOOD URGENCY PREDICTION
# --------------------------------------------------
# Features used by the model:
# 1. Hours remaining before expiry
# 2. Food type (0 = Raw, 1 = Cooked)
#
# Target classes:
# Low, Medium, High
#
# These small training examples are a starter dataset for the prototype.
# For a real deployment, replace them with a larger real-world food dataset.

urgency_training_data = [
    [48, 0], [42, 1], [36, 0], [32, 1], [30, 0],
    [28, 1], [26, 0], [24, 1], [22, 0], [20, 1],
    [18, 0], [16, 1], [14, 0], [12, 1], [10, 0],
    [9, 1], [8, 0], [7, 1], [6, 0], [5, 1],
    [4, 0], [3, 1], [2, 0], [1, 1], [0.5, 0],
    [60, 1], [54, 0], [50, 1], [45, 0], [40, 1],
    [15, 1], [11, 0], [9.5, 0], [7.5, 1],
    [5.5, 0], [4.5, 1], [2.5, 0]
]

urgency_training_labels = [
    "Low", "Low", "Low", "Low", "Low",
    "Low", "Low", "Low", "Low", "Low",
    "Medium", "Medium", "Medium", "Medium", "Medium",
    "Medium", "Medium", "Medium", "Medium", "High",
    "High", "High", "High", "High", "High",
    "Low", "Low", "Low", "Low", "Low",
    "Medium", "Medium", "Medium", "Medium",
    "High", "High", "High"
]

urgency_model = LogisticRegression(max_iter=1000)
urgency_model.fit(urgency_training_data, urgency_training_labels)


def calculate_hours_left(expiry_date):
    """Convert the stored expiry date into hours remaining."""
    if not expiry_date:
        return 0

    try:
        expiry_text = str(expiry_date).strip()

        # HTML <input type="date"> normally sends YYYY-MM-DD.
        # Treat that date as the end of the day.
        if len(expiry_text) == 10:
            expiry = datetime.strptime(expiry_text, "%Y-%m-%d")
            expiry = expiry.replace(hour=23, minute=59, second=59)
        else:
            # Support datetime-local values such as YYYY-MM-DDTHH:MM.
            expiry = datetime.fromisoformat(expiry_text)

        hours_left = (expiry - datetime.now()).total_seconds() / 3600
        return round(hours_left, 2)

    except (ValueError, TypeError):
        return 0


def predict_food_urgency(expiry_date, food_type):
    """Predict Low/Medium/High urgency for a FoodBridge listing."""
    hours_left = calculate_hours_left(expiry_date)

    # The model uses 0 for Raw and 1 for Cooked.
    food_type_value = 1 if str(food_type).strip().lower() == "cooked" else 0

    prediction = urgency_model.predict([[hours_left, food_type_value]])[0]
    probability = max(urgency_model.predict_proba([[hours_left, food_type_value]])[0]) * 100

    return prediction, round(probability, 2), hours_left


app = Flask(__name__)

# Change this to a long random secret before deployment
app.secret_key = os.environ.get("SECRET_KEY", "foodbridge-dev-secret-change-me")

# MongoDB connection
client = MongoClient("mongodb://localhost:27017")
db = client["FoodBridge"]

surplus = db["surplus"]
donarregdb = db["DonarRegistration"]
donarlogdb = db["Donarlog"]

# NGO collections (create automatically when first used)
ngoregdb = db["NgoRegistration"]
requestsdb = db["pickup_requests"]


def donor_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if (
            session.get("role") != "donor"
            or not session.get("donor_id")
        ):
            flash("Please login as a donor first.", "error")
            return redirect(url_for("donorloginroute"))

        return function(*args, **kwargs)

    return wrapper

@app.route("/")
def home():
    return render_template("role_selection.html")

@app.route("/donarregistration")
def donarregistration():
    return render_template("donarreg.html")


@app.route("/donarreg", methods=["GET", "POST"])
def donarreg():

    if request.method == "POST":

        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        donartype = request.form["donartype"]
        address = request.form["address"].strip()

        # Check if email already exists
        existing_user = donarregdb.find_one({"email": email})

        if existing_user:
            return "Email already registered. Please login."

        # Hash password instead of storing plain text
        hashed_password = generate_password_hash(password)

        donarregdb.insert_one({
            "name": name,
            "email": email,
            "password": hashed_password,
            "donartype": donartype,
            "address": address,
            "phone": request.form.get("phone", ""),
            "city": request.form.get("city", ""),
            "created_at": datetime.now(),
            "role": "donor"
        })

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("donorloginroute"))

    return render_template("donarreg.html")

@app.route("/donorloginroute")
def donorloginroute():
    return render_template("donarlog.html")


@app.route("/donarlogin", methods=["GET", "POST"])
def donarlogin():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        password = request.form["password"]

        donor = donarregdb.find_one({"email": email})

        if donor and check_password_hash(donor["password"], password):

            session.clear()

            session["donor_id"] = str(donor["_id"])
            session["name"] = donor["name"]
            session["role"] = "donor"

            return redirect(url_for("donor_dashboard"))

        flash("Invalid email or password.", "error")
        return redirect(url_for("donorloginroute"))

    return render_template("donarlog.html")


@app.route("/donor/dashboard")
@donor_required
def donor_dashboard():

    donor_id = ObjectId(session["donor_id"])

    # Get this donor's food listings
    total_listings = surplus.count_documents({
        "donor_id": donor_id
    })

    available_food = surplus.count_documents({
        "donor_id": donor_id,
        "status": "Available"
    })

    # Requests received for this donor's food
    food_ids = list(surplus.find(
        {"donor_id": donor_id},
        {"_id": 1}
    ))

    food_id_list = [food["_id"] for food in food_ids]

    pending_requests = requestsdb.count_documents({
        "food_id": {"$in": food_id_list},
        "status": "Pending"
    })

    completed_donations = requestsdb.count_documents({
        "food_id": {"$in": food_id_list},
        "status": "Completed"
    })

    recent_food = list(
        surplus.find({"donor_id": donor_id})
        .sort("_id", -1)
        .limit(5)
    )

    # Recalculate urgency so the dashboard reflects the current time.
    for food in recent_food:
        (
            food["urgency"],
            food["urgency_confidence"],
            food["hours_left"]
        ) = predict_food_urgency(
            food.get("expiry_date"),
            food.get("food_type", "")
        )

    recent_requests = list(
        requestsdb.find({
            "food_id": {"$in": food_id_list}
        })
        .sort("_id", -1)
        .limit(5)
    )

    # Add food and NGO names to the request records
    for req in recent_requests:

        food = surplus.find_one({"_id": req["food_id"]})

        req["food_name"] = food.get("food_name", "Food") if food else "Food"

        ngo = ngoregdb.find_one({
            "_id": req.get("ngo_id")
        })

        req["ngo_name"] = ngo.get("name", "NGO") if ngo else "NGO"

    return render_template(
        "donor_dashboard.html",
        total_listings=total_listings,
        available_food=available_food,
        pending_requests=pending_requests,
        completed_donations=completed_donations,
        recent_food=recent_food,
        recent_requests=recent_requests
    )


@app.route("/add-food", methods=["GET", "POST"])
@donor_required
def add_food():

    if request.method == "POST":

        food_name = request.form["food_name"].strip()
        quantity = request.form["quantity"].strip()
        food_type = request.form.get("food_type", "")
        expiry_date = request.form["expiry_date"]
        address = request.form.get("address", "").strip()

        # ML prediction for the new food listing
        urgency, urgency_confidence, hours_left = predict_food_urgency(
            expiry_date, food_type
        )

        surplus.insert_one({
            "donor_id": ObjectId(session["donor_id"]),
            "food_name": food_name,
            "quantity": quantity,
            "food_type": food_type,
            "expiry_date": expiry_date,
            "address": address,
            "status": "Available",
            "urgency": urgency,
            "urgency_confidence": urgency_confidence,
            "hours_left_at_listing": hours_left,
            "created_at": datetime.now()
        })

        flash("Food added successfully!", "success")
        return redirect(url_for("donor_dashboard"))

    return render_template("add_food.html")


@app.route("/donor/requests")
@donor_required
def incoming_requests():

    donor_id = ObjectId(session["donor_id"])

    # Only retrieve requests for food belonging to this donor
    donor_food = list(surplus.find(
        {"donor_id": donor_id},
        {"_id": 1}
    ))

    food_ids = [food["_id"] for food in donor_food]

    requests = list(
        requestsdb.find({
            "food_id": {"$in": food_ids}
        }).sort("_id", -1)
    )

    for req in requests:

        food = surplus.find_one({"_id": req["food_id"]})

        req["food_name"] = food.get("food_name", "Food") if food else "Food"

        ngo = ngoregdb.find_one({
            "_id": req.get("ngo_id")
        })

        req["ngo_name"] = ngo.get("name", "NGO") if ngo else "NGO"

    return render_template(
        "incoming_requests.html",
        requests=requests
    )


@app.route("/donor/update-request/<request_id>", methods=["POST"])
@donor_required
def update_request(request_id):

    donor_id = ObjectId(session["donor_id"])

    try:
        req_id = ObjectId(request_id)
    except Exception:
        return "Invalid request ID", 400

    new_status = request.form.get("status")

    if new_status not in ["Accepted", "Rejected"]:
        return "Invalid status", 400

    pickup_request = requestsdb.find_one({
        "_id": req_id
    })

    if not pickup_request:
        return "Request not found", 404

    # Verify that the food belongs to the logged-in donor
    food = surplus.find_one({
        "_id": pickup_request["food_id"],
        "donor_id": donor_id
    })

    if not food:
        return "You are not authorized to update this request.", 403

    if pickup_request["status"] != "Pending":
        flash("This request has already been processed.", "error")
        return redirect(url_for("incoming_requests"))

    requestsdb.update_one(
        {
            "_id": req_id,
            "status": "Pending"
        },
        {
            "$set": {
                "status": new_status,
                "updated_at": datetime.now()
            }
        }
    )

    if new_status == "Accepted":

        # Reserve the food so it is not listed as available
        surplus.update_one(
            {"_id": food["_id"]},
            {"$set": {"status": "Reserved"}}
        )

    flash(f"Request {new_status.lower()} successfully.", "success")

    return redirect(url_for("incoming_requests"))

@app.route("/donor/history")
@donor_required
def donation_history():

    donor_id = ObjectId(session["donor_id"])

    donor_food = list(surplus.find(
        {"donor_id": donor_id},
        {"_id": 1}
    ))

    food_ids = [food["_id"] for food in donor_food]

    history = list(
        requestsdb.find({
            "food_id": {"$in": food_ids},
            "status": "Completed"
        }).sort("_id", -1)
    )

    total_quantity = 0
    ngo_ids = set()

    for item in history:

        food = surplus.find_one({"_id": item["food_id"]})

        if food:
            item["food_name"] = food.get("food_name", "Food")
            item["quantity"] = food.get("quantity", "N/A")

        ngo = ngoregdb.find_one({
            "_id": item.get("ngo_id")
        })

        item["ngo_name"] = ngo.get("name", "NGO") if ngo else "NGO"

        completed_at = item.get("picked_up_at")

        if completed_at:
            item["completed_date"] = completed_at.strftime("%d %b %Y, %I:%M %p")
        else:
            item["completed_date"] = "—"

        if item.get("ngo_id"):
            ngo_ids.add(str(item["ngo_id"]))

    return render_template(
        "donation_history.html",
        history=history,
        total_quantity=total_quantity,
        total_ngos=len(ngo_ids),
        meals_served=0
    )


@app.route("/donor/profile", methods=["GET", "POST"])
@donor_required
def donor_profile():

    donor_id = ObjectId(session["donor_id"])

    donor = donarregdb.find_one({
        "_id": donor_id
    })

    if not donor:
        session.clear()
        return redirect(url_for("donorloginroute"))

    if request.method == "POST":

        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form.get("phone", "").strip()
        donartype = request.form.get("donor_type", "")
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()

        # Do not allow duplicate email addresses
        existing_email = donarregdb.find_one({
            "email": email,
            "_id": {"$ne": donor_id}
        })

        if existing_email:
            flash("This email is already registered.", "error")
            return redirect(url_for("donor_profile"))

        update_data = {
            "name": name,
            "email": email,
            "phone": phone,
            "donartype": donartype,
            "address": address,
            "city": city
        }

        new_password = request.form.get("password", "").strip()

        if new_password:
            update_data["password"] = generate_password_hash(
                new_password
            )

        donarregdb.update_one(
            {"_id": donor_id},
            {"$set": update_data}
        )

        session["name"] = name

        flash("Profile updated successfully.", "success")
        return redirect(url_for("donor_profile"))

    return render_template(
        "donor_profile.html",
        donor=donor
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

from functools import wraps


# NGO route protection
def ngo_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if (
            session.get("role") != "ngo"
            or not session.get("ngo_id")
        ):
            flash("Please login as an NGO first.", "error")
            return redirect(url_for("ngo_login"))

        return function(*args, **kwargs)

    return wrapper


# ---------------- NGO REGISTER ----------------

@app.route("/ngo/register", methods=["GET", "POST"])
def ngo_register():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()

        if not name or not email or not password or not address:
            flash("Please fill all required fields.", "error")
            return redirect(url_for("ngo_register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("ngo_register"))

        if ngoregdb.find_one({"email": email}):
            flash("NGO email already registered.", "error")
            return redirect(url_for("ngo_login"))

        if donarregdb.find_one({"email": email}):
            flash("This email is already registered as a donor.", "error")
            return redirect(url_for("ngo_register"))

        ngoregdb.insert_one({
            "name": name,
            "email": email,
            "password": generate_password_hash(password),
            "phone": phone,
            "address": address,
            "city": city,
            "role": "ngo",
            "created_at": datetime.now()
        })

        flash("NGO registered successfully. Please login.", "success")
        return redirect(url_for("ngo_login"))

    return render_template("ngo_register.html")


# ---------------- NGO LOGIN ----------------

@app.route("/ngo/login", methods=["GET", "POST"])
def ngo_login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        ngo = ngoregdb.find_one({"email": email})

        if ngo and check_password_hash(ngo["password"], password):

            session.clear()

            session["ngo_id"] = str(ngo["_id"])
            session["ngo_name"] = ngo["name"]
            session["role"] = "ngo"

            return redirect(url_for("ngo_dashboard"))

        flash("Invalid NGO email or password.", "error")
        return redirect(url_for("ngo_login"))

    return render_template("ngo_login.html")


# ---------------- NGO DASHBOARD ----------------

@app.route("/ngo/dashboard")
@ngo_required
def ngo_dashboard():

    ngo_id = ObjectId(session["ngo_id"])

    total_requests = requestsdb.count_documents({
        "ngo_id": ngo_id
    })

    pending_requests = requestsdb.count_documents({
        "ngo_id": ngo_id,
        "status": "Pending"
    })

    accepted_requests = requestsdb.count_documents({
        "ngo_id": ngo_id,
        "status": "Accepted"
    })

    completed_requests = requestsdb.count_documents({
        "ngo_id": ngo_id,
        "status": "Completed"
    })

    return render_template(
        "ngo_dashboard.html",
        ngo_name=session["ngo_name"],
        total_requests=total_requests,
        pending_requests=pending_requests,
        accepted_requests=accepted_requests,
        completed_requests=completed_requests
    )


# ---------------- NGO BROWSE FOOD ----------------

@app.route("/ngo/food")
@ngo_required
def ngo_food():

    food_items = list(
        surplus.find({
            "status": "Available"
        }).sort("created_at", -1)
    )

    # Recalculate urgency for NGO food listings.
    for food in food_items:
        (
            food["urgency"],
            food["urgency_confidence"],
            food["hours_left"]
        ) = predict_food_urgency(
            food.get("expiry_date"),
            food.get("food_type", "")
        )

    return render_template(
        "ngo_food.html",
        food_items=food_items
    )


# ---------------- NGO FOOD DETAILS ----------------

@app.route("/ngo/food/<food_id>")
@ngo_required
def ngo_food_details(food_id):

    if not ObjectId.is_valid(food_id):
        flash("Invalid food listing.", "error")
        return redirect(url_for("ngo_food"))

    food = surplus.find_one({
        "_id": ObjectId(food_id),
        "status": "Available"
    })

    if not food:
        flash("Food is no longer available.", "error")
        return redirect(url_for("ngo_food"))

    return render_template(
        "ngo_food_details.html",
        food=food
    )


# ---------------- NGO REQUEST FOOD ----------------


@app.route("/ngo/request-food/<food_id>", methods=["POST"])
@ngo_required
def ngo_request_food(food_id):

    if not ObjectId.is_valid(food_id):
        flash("Invalid food listing.", "error")
        return redirect(url_for("ngo_food"))

    food = surplus.find_one({
        "_id": ObjectId(food_id),
        "status": "Available"
    })

    if not food:
        flash("This food is no longer available.", "error")
        return redirect(url_for("ngo_food"))

    # Get quantity as text
    quantity = request.form.get("quantity", "").strip()

    # Check if quantity is empty
    if not quantity:
        flash("Please enter a valid quantity.", "error")
        return redirect(url_for(
            "ngo_food_details", food_id=food_id
        ))

    # Get NGO ID
    ngo_id = ObjectId(session["ngo_id"])

    # Check for existing active request
    existing = requestsdb.find_one({
        "food_id": ObjectId(food_id),
        "ngo_id": ngo_id,
        "status": {"$in": ["Pending", "Accepted"]}
    })

    if existing:
        flash(
            "You already have an active request for this food.",
            "error"
        )
        return redirect(url_for("ngo_my_requests"))

    # Save request
    requestsdb.insert_one({
        "food_id": ObjectId(food_id),
        "donor_id": food["donor_id"],
        "ngo_id": ngo_id,
        "quantity": quantity,
        "requested_quantity": quantity,
        "message": request.form.get("message", "").strip(),
        "status": "Pending",
        "requested_at": datetime.now()
    })

    flash("Food request sent successfully.", "success")

    return redirect(url_for("ngo_my_requests"))

# ---------------- NGO MY REQUESTS ----------------

@app.route("/ngo/my-requests")
@ngo_required
def ngo_my_requests():

    ngo_id = ObjectId(session["ngo_id"])

    requests_list = list(
        requestsdb.find({
            "ngo_id": ngo_id
        }).sort("requested_at", -1)
    )

    for req in requests_list:

        food = surplus.find_one({
            "_id": req["food_id"]
        })

        req["food_name"] = (
            food.get("food_name", "Food unavailable")
            if food else "Food unavailable"
        )

    return render_template(
        "ngo_food_requests.html",
        requests=requests_list
    )


# ---------------- NGO CANCEL REQUEST ----------------

@app.route("/ngo/cancel-request/<request_id>", methods=["POST"])
@ngo_required
def ngo_cancel_request(request_id):

    if not ObjectId.is_valid(request_id):
        flash("Invalid request.", "error")
        return redirect(url_for("ngo_my_requests"))

    result = requestsdb.update_one({
        "_id": ObjectId(request_id),
        "ngo_id": ObjectId(session["ngo_id"]),
        "status": "Pending"
    }, {
        "$set": {
            "status": "Cancelled",
            "updated_at": datetime.now()
        }
    })

    if result.modified_count:
        flash("Request cancelled successfully.", "success")
    else:
        flash("Only pending requests can be cancelled.", "error")

    return redirect(url_for("ngo_my_requests"))


@app.route("/ngo/confirm-pickup/<request_id>", methods=["POST"])
@ngo_required
def ngo_confirm_pickup(request_id):

    if not ObjectId.is_valid(request_id):
        flash("Invalid pickup request.", "error")
        return redirect(url_for("ngo_my_requests"))

    pickup_request = requestsdb.find_one({
        "_id": ObjectId(request_id),
        "ngo_id": ObjectId(session["ngo_id"]),
        "status": "Accepted"
    })

    if not pickup_request:
        flash("Request not found or not accepted.", "error")
        return redirect(url_for("ngo_my_requests"))

    requestsdb.update_one(
        {"_id": ObjectId(request_id)},
        {
            "$set": {
                "status": "Completed",
                "picked_up_at": datetime.now()
            }
        }
    )

    flash("Pickup confirmed successfully!", "success")

    return redirect(url_for("ngo_my_requests"))
# ---------------- NGO LOGOUT ----------------


@app.route("/food-expiry-advice/<food_id>", methods=["POST"])
@donor_required
def food_expiry_advice(food_id):

    # Validate food ID
    if not ObjectId.is_valid(food_id):
        flash("Invalid food listing.", "error")
        return redirect(url_for("donor_dashboard"))

    # Find food belonging to the logged-in donor
    food = surplus.find_one({
        "_id": ObjectId(food_id),
        "donor_id": ObjectId(session["donor_id"])
    })

    if not food:
        flash("Food item not found.", "error")
        return redirect(url_for("donor_dashboard"))

    # Get food details from MongoDB
# Get food details from MongoDB
    foodname = food.get("food_name", "Unknown food")
    category = food.get("category", "Not specified")
    quantity = food.get("quantity", "Not specified")
    expiry_date = str(food.get("expiry_date", "Not specified"))



    today = datetime.now().strftime("%Y-%m-%d")

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
contents=f"""
You are FoodBridge's AI Food Safety Assistant.

Food: {foodname}
Category: {category}
Expiry: {expiry_date}
Today: {today}

Return concise Markdown with exactly:

## Food Safety Status
State safety precautions and whether expired food should be discarded.

## What Should I Do?
Give 3 safe handling steps.

## Safe Disposal
Give 3 disposal tips.

## How to Prevent Food Waste
Give 3 prevention tips.

Never recommend eating or donating unsafe food.
Never suggest tasting or relying on smell or appearance.
Return ONLY Markdown, no HTML.
"""
        )

        result = markdown.markdown(response.text)

    except Exception as e:
        print("Gemini API Error:", e)

        flash(
            "Unable to generate food safety advice. Please try again.",
            "error"
        )

        return redirect(url_for("donor_dashboard"))

    return render_template(
        "food_expiry_advice.html",
        foodname=foodname,
        category=category,
        expiry_date=expiry_date,
        result=result
    )

@app.route("/ngo/logout")
def ngo_logout():
    session.clear()
    return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(debug=True)