"""Rows that make the environment produce each notification variant.

The five variants in QA_TEST_PLAN section 3 are not five screens the client can
be asked for. They are five different answers the server computes from rows in
`purchases`, signs into a token, and hands over — so the only honest way to see
them is to put the rows there and let the server do its own arithmetic.

What the server actually does (backend notification/db-operations.ts), reduced
to what has to be true for each variant:

    a purchase counts as `new`       reportedPurchaseStatus IS NULL
                                     AND status <> 'CORRECTED'
                                     AND internalViewOnly <> 1
    it counts as `eligible`          status = 'READY'
    it counts toward `total`         any row matched to the user
    `expiredAt` appears              a READY row's approvalTime is inside the
                                     122-day reminding period, and no wallet
    `promptPairing` is true          no wallet, and there is a `new` row

Rows are matched to a person through `retailerClicks`, not through `purchases`
— the join is `purchases.clickId -> retailerClicks.id`, and it is the click
that carries `walletAddress` and `platformId`. For a user with no wallet the
backend writes the *user id* into that column, prefixed. So seeding a variant
means one click plus one or two purchases against it.

Everything written here is tagged, and `clean` removes exactly what was
written. Writes refuse any database whose name looks like production.
"""
import os
import uuid
from datetime import datetime, timedelta

from bring import config, db

# Ecko. Read from the environment's own `platforms` table rather than assumed;
# this is the fallback for when the lookup cannot run.
PLATFORM_IDS = {"ecko": 10, "yoroi": 4, "gero": 5, "cspr": 3, "argent": 7,
                "fuel": 9, "nightly": 11, "solflare": 12}

# What the backend prepends to a user id when it records a click for someone
# with no wallet (`processedUser`, DEDICATED_PREFIX_STR). Overridable because
# it is an environment variable on the deployed lambda, not a constant.
USER_PREFIX = os.getenv("BRING_USER_PREFIX", "bring_u-")

# Stamped on every row this module writes, so cleanup removes its own rows and
# nothing else — a seeder that deletes by user id would take real rows with it.
TAG = "e2e-seed"

TOKEN = "ADA"
DAY = timedelta(days=1)


def processed_user(user_id: str) -> str:
    """The value the backend stores in `retailerClicks.walletAddress` for a
    wallet-less user — the prefix plus the id with its dashes removed."""
    bare = (user_id or "").replace("-", "")
    return bare if bare.startswith(USER_PREFIX) else USER_PREFIX + bare


def platform_id(database: str, platform: str = "ecko") -> int:
    """The environment's own id for this platform, by name."""
    try:
        rows = db.query("SELECT id FROM platforms WHERE LOWER(identifier) = %s "
                        "OR LOWER(name) = %s LIMIT 1",
                        (platform.lower(), platform.lower()), database=database)
        if rows:
            return int(rows[0]["id"])
    except Exception:
        pass
    return PLATFORM_IDS.get(platform.lower(), 10)


def a_retailer(database: str) -> str:
    """Any retailer id, to hang the click on. The offer is not under test here."""
    rows = db.query("SELECT id FROM retailers LIMIT 1", database=database)
    if not rows:
        raise db.NoDatabase(f"{database} has no retailers to attach a click to")
    return rows[0]["id"]


# ── the five shapes ─────────────────────────────────────────────────
# Each is a list of purchases to write against one click. `status` and
# `reported` are what decide which sums the row lands in; `ready_age_days`
# places the approval inside the reminding period so a deadline appears.

VARIANTS = {
    # A wallet is connected, so the click is keyed by the address and the
    # server has nothing to prompt for: "you just earned X", button Details.
    "reward_approval": {
        "wallet": True,
        "purchases": [{"status": "APPROVED", "reported": None, "amount": 0.42}],
    },
    # No wallet, one new purchase, nothing claimable and nothing expiring.
    "walletless_1": {
        "wallet": False,
        "purchases": [{"status": "APPROVED", "reported": None, "amount": 0.42}],
    },
    # Same, plus something already approved long enough ago to be expiring:
    # the deadline line appears.
    "walletless_2": {
        "wallet": False,
        "purchases": [
            {"status": "APPROVED", "reported": None, "amount": 0.42},
            {"status": "READY", "reported": "APPROVED", "amount": 0.31,
             "ready_age_days": 30},
        ],
    },
    # New *and* claimable: the button becomes Claim.
    "walletless_3": {
        "wallet": False,
        "purchases": [
            {"status": "APPROVED", "reported": None, "amount": 0.42},
            {"status": "READY", "reported": None, "amount": 0.55,
             "ready_age_days": 30},
        ],
    },
    # Claimable only — nothing new — which is what puts Stop Reminding on the
    # notification alongside Claim.
    #
    # With no new purchase there is nothing to announce, so the server would
    # normally stay quiet: `showNotification = newPurchases.length > 0`. This
    # variant exists on the *reminder* path instead, which needs a reminder row
    # older than the seven-day interval before `getReminder` counts it. Hence
    # `reminder_days` — measured against the server returning
    # showNotification=False without it.
    "walletless_4": {
        "wallet": False,
        "reminder_days": 8,
        "purchases": [
            {"status": "READY", "reported": "APPROVED", "amount": 0.55,
             "ready_age_days": 30},
        ],
    },
}


def seed(variant: str, *, database: str, user_id: str, wallet_address: str,
         platform: str = "ecko") -> dict:
    """Write the rows for *variant*. Returns what was written, for cleanup."""
    shape = VARIANTS.get(variant)
    if not shape:
        raise ValueError(f"unknown variant {variant!r}; "
                         f"known: {', '.join(sorted(VARIANTS))}")

    db.refuse_if_production(database)

    owner = wallet_address if shape["wallet"] else processed_user(user_id)
    click_id = str(uuid.uuid4())
    now = datetime.utcnow()

    db.execute(
        "INSERT INTO retailerClicks "
        "(id, retailerId, walletAddress, tokenSymbol, platformId, "
        " affiliateNetworkCode, createdTime, countryCode, activationSource) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (click_id, a_retailer(database), owner, TOKEN,
         platform_id(database, platform), "E2", now, "US", TAG),
        database=database,
    )

    written = []
    for index, row in enumerate(shape["purchases"]):
        purchase_id = str(uuid.uuid4())
        approval = (now - row.get("ready_age_days", 0) * DAY
                    if row["status"] == "READY" else None)
        db.execute(
            "INSERT INTO purchases "
            "(id, clickId, extPurchaseId, status, tokenAmount, purchaseAmount, "
            " purchaseDate, approvalTime, reportedPurchaseStatus, "
            " internalViewOnly, isDemo) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0)",
            (purchase_id, click_id, f"{TAG}-{variant}-{index}", row["status"],
             row["amount"], 10.0, now - DAY, approval, row["reported"]),
            database=database,
        )
        written.append(purchase_id)

    reminder_days = shape.get("reminder_days")
    if reminder_days:
        # `reminders.userId` is the bare id, dashes stripped — the column is
        # varchar(32), which is a uuid without them and no room for a prefix.
        bare = (user_id or "").replace("-", "")
        when = now - reminder_days * DAY
        db.execute(
            "INSERT INTO reminders (userId, platformId, reminderTime, createdTime) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE reminderTime = VALUES(reminderTime)",
            (bare, platform_id(database, platform), when, when),
            database=database,
        )

    return {"variant": variant, "click_id": click_id, "purchase_ids": written,
            "owner": owner, "database": database,
            "reminder_user": (user_id or "").replace("-", "") if reminder_days else None}


def clean(seeded: dict) -> None:
    """Remove exactly the rows `seed` wrote. Never touches anything else."""
    if not seeded:
        return
    database = seeded["database"]
    db.refuse_if_production(database)
    db.execute("DELETE FROM purchases WHERE clickId = %s",
               (seeded["click_id"],), database=database)
    db.execute("DELETE FROM retailerClicks WHERE id = %s",
               (seeded["click_id"],), database=database)
    if seeded.get("reminder_user"):
        db.execute("DELETE FROM reminders WHERE userId = %s",
                   (seeded["reminder_user"],), database=database)


def clean_all(database: str) -> int:
    """Remove every row this module has ever written to *database*.

    For the run that crashed before its cleanup, so the next one does not
    inherit rewards it did not create.
    """
    db.refuse_if_production(database)
    removed = db.execute(
        "DELETE p FROM purchases p JOIN retailerClicks rc ON p.clickId = rc.id "
        "WHERE rc.activationSource = %s", (TAG,), database=database)
    db.execute("DELETE FROM retailerClicks WHERE activationSource = %s",
               (TAG,), database=database)
    return removed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed a notification variant")
    parser.add_argument("variant", nargs="?", choices=sorted(VARIANTS))
    parser.add_argument("--database", default=None)
    parser.add_argument("--user-id", default=str(uuid.uuid4()))
    parser.add_argument("--wallet", default=config.WALLET)
    parser.add_argument("--clean-all", action="store_true")
    args = parser.parse_args()

    target = args.database or db.database_for()
    if args.clean_all:
        print(f"removed {clean_all(target)} seeded purchase(s) from {target}")
    elif args.variant:
        print(seed(args.variant, database=target, user_id=args.user_id,
                   wallet_address=args.wallet))
    else:
        parser.error("give a variant, or --clean-all")
