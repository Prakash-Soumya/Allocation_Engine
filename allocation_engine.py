import pandas as pd
import argparse
from ortools.sat.python import cp_model
from datetime import timedelta

# =====================================================
# INPUT FILES
# =====================================================
parser = argparse.ArgumentParser(
    description="Allocation Engine"
)

parser.add_argument(
    "run_date",
    help="Date suffix in YYYYMMDD format"
)

args = parser.parse_args()

RUN_DATE = args.run_date

DEMAND_FILE = f"Demand_{RUN_DATE}.csv"
SKILL_FILE = f"SkillGroup_{RUN_DATE}.csv"
SUPPLY_FILE = f"Supply_{RUN_DATE}.csv"

ALLOC_FILE = f"Allocation_{RUN_DATE}.csv"
EXCEPTION_FILE = f"Exception_{RUN_DATE}.csv"

print(f"Using Demand File    : {DEMAND_FILE}")
print(f"Using Skill File     : {SKILL_FILE}")
print(f"Using Supply File    : {SUPPLY_FILE}")

# =====================================================
# LOAD DATA
# =====================================================

demand_df = pd.read_csv(DEMAND_FILE)
skill_df = pd.read_csv(SKILL_FILE)
supply_df = pd.read_csv(SUPPLY_FILE)

# =====================================================
# STANDARDIZE COLUMNS
# =====================================================

def clean_columns(df):
    df.columns = (
        df.columns
        .str.strip()
        .str.replace(" ", "_")
    )
    return df

demand_df = clean_columns(demand_df)
skill_df = clean_columns(skill_df)
supply_df = clean_columns(supply_df)

# =====================================================
# PARSE DATES
# =====================================================

for df in [demand_df, supply_df]:

    for col in df.columns:

        if "Start" in col:
            df[col] = pd.to_datetime(df[col],format="%d/%m/%Y %H:%M",errors="coerce")

        if "End" in col:
            df[col] = pd.to_datetime(df[col],format="%d/%m/%Y %H:%M",errors="coerce")

# =====================================================
# USER MASTER
# =====================================================

user_skill = {}

for _, r in skill_df.iterrows():

    user_skill[r["User_ID"]] = {
        "skill": str(r["Skill_Group"]).strip(),
        "gender": str(r["Gender"]).strip()
    }

# =====================================================
# SUPPLY LOOKUP
# =====================================================

user_supply = {}

for user in supply_df["User_ID"].unique():

    user_supply[user] = supply_df[
        supply_df["User_ID"] == user
    ].copy()

# =====================================================
# CHECK FULL AVAILABILITY
# =====================================================

def has_full_availability(
        user,
        demand_start,
        demand_end,
        demand_type,
        demand_location):

    if user not in user_supply:
        return False

    slots = user_supply[user]

    current = demand_start

    while current < demand_end:

        slot_end = current + timedelta(minutes=30)

        found = False

        for _, s in slots.iterrows():

            if not (
                s["Start_DateTime"] <= current and
                s["End_DateTime"] >= slot_end
            ):
                continue

            supply_type = str(s["Type"]).strip()

            if demand_type == "Online":

                if supply_type not in ["Online", "All"]:
                    continue

            else:

                if supply_type not in ["In-Person", "All"]:
                    continue

                if str(s["Location"]).strip() != str(demand_location).strip():
                    continue

            found = True
            break

        if not found:
            return False

        current = slot_end

    return True

# =====================================================
# EXCEPTION HIERARCHY
# =====================================================

def determine_exception_reason(demand):

    skill_users = skill_df[
        skill_df["Skill_Group"].astype(str).str.strip()
        ==
        str(demand["Skill_Group"]).strip()
    ]

    if len(skill_users) == 0:
        return "No matching skill group found"

    gender_pref = str(demand["Gender_Pref"]).strip()

    gender_users = skill_users

    if gender_pref != "N/A":

        gender_users = skill_users[
            skill_users["Gender"].astype(str).str.strip()
            == gender_pref
        ]

        if len(gender_users) == 0:
            return "Skill available but wrong gender"

    if demand["Type"] == "In-Person":

        location_found = False

        for _, u in gender_users.iterrows():

            user = u["User_ID"]

            if user not in user_supply:
                continue

            supplies = user_supply[user]

            if any(
                str(loc).strip() ==
                str(demand["Location"]).strip()
                for loc in supplies["Location"]
            ):
                location_found = True
                break

        if not location_found:
            return "Gender available but wrong location"

    availability_found = False

    for _, u in gender_users.iterrows():

        user = u["User_ID"]

        if has_full_availability(
            user,
            demand["Start_DateTime"],
            demand["End_DateTime"],
            demand["Type"],
            demand["Location"]
        ):
            availability_found = True
            break

    if not availability_found:
        return "Location available but no time capacity"

    return "User already allocated elsewhere"

# =====================================================
# CANDIDATE GENERATION
# =====================================================

candidate_map = {}

for idx, demand in demand_df.iterrows():

    candidates = []

    skill_required = str(
        demand["Skill_Group"]
    ).strip()

    gender_required = str(
        demand["Gender_Pref"]
    ).strip()

    for user, details in user_skill.items():

        # Skill

        if details["skill"] != skill_required:
            continue

        # Gender

        if gender_required != "N/A":

            if details["gender"] != gender_required:
                continue

        # Availability

        if not has_full_availability(
                user,
                demand["Start_DateTime"],
                demand["End_DateTime"],
                demand["Type"],
                demand["Location"]):
            continue

        candidates.append(user)

    candidate_map[idx] = candidates

# =====================================================
# OR MODEL
# =====================================================

model = cp_model.CpModel()

assign = {}

for demand_idx, users in candidate_map.items():

    for user in users:

        assign[(demand_idx, user)] = model.NewBoolVar(
            f"a_{demand_idx}_{user}"
        )

# =====================================================
# ONE RESOURCE MAX PER APPOINTMENT
# =====================================================

allocated_vars = []

for demand_idx, users in candidate_map.items():

    allocation_var = model.NewBoolVar(
        f"allocated_{demand_idx}"
    )

    if len(users) > 0:

        model.Add(
            sum(assign[(demand_idx, u)] for u in users)
            == allocation_var
        )

    else:

        model.Add(allocation_var == 0)

    allocated_vars.append(allocation_var)

# =====================================================
# NO OVERLAPPING BOOKINGS
# =====================================================

all_users = skill_df["User_ID"].unique()

for user in all_users:

    user_demands = []

    for d_idx, users in candidate_map.items():

        if user in users:
            user_demands.append(d_idx)

    for i in range(len(user_demands)):

        for j in range(i + 1, len(user_demands)):

            d1 = demand_df.loc[user_demands[i]]
            d2 = demand_df.loc[user_demands[j]]

            overlapping = (
                d1["Start_DateTime"] < d2["End_DateTime"] and
                d2["Start_DateTime"] < d1["End_DateTime"]
            )

            if overlapping:

                model.Add(
                    assign[(user_demands[i], user)] +
                    assign[(user_demands[j], user)]
                    <= 1
                )

# =====================================================
# APPOINTMENT DURATION
# =====================================================

duration_minutes = {}

for idx, demand in demand_df.iterrows():

    duration_minutes[idx] = int(
        (
            demand["End_DateTime"]
            -
            demand["Start_DateTime"]
        ).total_seconds() / 60
    )

# =====================================================
# WORKLOAD BALANCING
# =====================================================

user_load = {}

max_possible_minutes = sum(duration_minutes.values())

for user in all_users:

    load = model.NewIntVar(
        0,
        max_possible_minutes,
        f"load_{user}"
    )

    workload_terms = []

    for d_idx, users in candidate_map.items():

        if user in users:

            workload_terms.append(
                assign[(d_idx, user)]
                * duration_minutes[d_idx]
            )

    if workload_terms:
        model.Add(load == sum(workload_terms))
    else:
        model.Add(load == 0)

    user_load[user] = load

max_possible_minutes = sum(duration_minutes.values())

max_load = model.NewIntVar(
    0,
    max_possible_minutes,
    "max_load"
)

min_load = model.NewIntVar(
    0,
    max_possible_minutes,
    "min_load"
)

load_spread = model.NewIntVar(
    0,
    max_possible_minutes,
    "load_spread"
)

model.AddMaxEquality(
    max_load,
    list(user_load.values())
)

model.AddMinEquality(
    min_load,
    list(user_load.values())
)

model.Add(
    load_spread == max_load - min_load
)


# =====================================================
# OBJECTIVE
# =====================================================

objective_terms = []

for (d_idx, user), var in assign.items():

    demand = demand_df.loc[d_idx]

    score = 0

    # Priority 1
    if str(demand["Gender_Pref"]).strip() != "N/A":
        score += 10000

    # Priority 2
    if str(demand["Type"]).strip() == "In-Person":
        score += 5000

    # Priority 3
    score += 1000

    objective_terms.append(score * var)

model.Maximize(
    1000000 * sum(allocated_vars)
    + sum(objective_terms)
    - 50 * load_spread
)

# =====================================================
# SOLVE
# =====================================================

solver = cp_model.CpSolver()

solver.parameters.max_time_in_seconds = 120

status = solver.Solve(model)

if status not in [
    cp_model.OPTIMAL,
    cp_model.FEASIBLE
]:
    print("No feasible solution found")

# =====================================================
# OUTPUTS
# =====================================================

allocations = []
exceptions = []

for idx, demand in demand_df.iterrows():

    allocated_user = None

    users = candidate_map.get(idx, [])

    for user in users:

        if solver.Value(
            assign[(idx, user)]
        ) == 1:

            allocated_user = user
            break

    if allocated_user:

        allocations.append({
            "Appointment_ID":
                demand["Appointment_ID"],
            "Start_DateTime":
                demand["Start_DateTime"],
            "End_DateTime":
                demand["End_DateTime"],
            "Type":
                demand["Type"],
            "Location":
                demand["Location"],
            "Gender_Pref":
                demand["Gender_Pref"],
            "User_ID":
                allocated_user
        })

    else:

        exceptions.append({
            "Appointment_ID":
                demand["Appointment_ID"],
            "Start_DateTime":
                demand["Start_DateTime"],
            "End_DateTime":
                demand["End_DateTime"],
            "Type":
                demand["Type"],
            "Location":
                demand["Location"],
            "Gender_Pref":
                demand["Gender_Pref"],
            "Exception_Reason":
                determine_exception_reason(demand)
        })

# =====================================================
# EXPORT
# =====================================================

allocation_df = pd.DataFrame(allocations)

exception_df = pd.DataFrame(exceptions)

allocation_df.to_csv(
    ALLOC_FILE,
    index=False
)

exception_df.to_csv(
    EXCEPTION_FILE,
    index=False
)

print()
print("Allocation complete")
print(f"Allocated Appointments : {len(allocation_df)}")
print(f"Exceptions             : {len(exception_df)}")
print(f"Output File            : {ALLOC_FILE}")
print(f"Exception File         : {EXCEPTION_FILE}")



