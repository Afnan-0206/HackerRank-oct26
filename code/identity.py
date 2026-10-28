"""
Identity Resolver for Ghost Data Still Exists.
Resolves current and historical user identities from user_identities.csv and user_audit_log.csv.
"""

import os
import pandas as pd
from typing import Dict, Optional, Set
try:
    from .models import UserIdentity
except (ImportError, ValueError):
    from models import UserIdentity


class IdentityResolver:
    @staticmethod
    def _resolve_dataset_dir(dataset_dir: str) -> str:
        candidates = [
            dataset_dir,
            os.path.abspath(dataset_dir),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dataset")),
            os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "dataset")),
            os.path.join(os.getcwd(), dataset_dir),
            os.path.join(os.getcwd(), "hackerrank-orchestrate-october26", dataset_dir),
        ]
        for c in candidates:
            if os.path.exists(os.path.join(c, "user_identities.csv")):
                return os.path.abspath(os.path.realpath(c))
        return os.path.abspath(os.path.realpath(dataset_dir))

    def __init__(self, dataset_dir: str):
        self.dataset_dir = self._resolve_dataset_dir(dataset_dir)
        self.identities_by_id: Dict[str, UserIdentity] = {}
        self.email_to_id: Dict[str, str] = {}
        self._load_identities()
        self._load_audit_log()

    def _load_identities(self):
        path = os.path.join(self.dataset_dir, "user_identities.csv")
        if not os.path.exists(path):
            return

        df = pd.read_csv(path)
        for _, row in df.iterrows():
            uid = str(row["user_id"]).strip()
            name = str(row["name"]).strip() if pd.notna(row["name"]) else ""
            email = str(row["email"]).strip() if pd.notna(row["email"]) else ""
            phone = str(row["phone"]).strip() if pd.notna(row["phone"]) else ""
            e_hash = str(row["email_hash"]).strip() if pd.notna(row["email_hash"]) else ""

            user = UserIdentity(
                user_id=uid,
                current_email=email,
                current_phone=phone,
                current_name=name,
                email_hash=e_hash,
                all_emails={email.lower()} if email else set(),
                all_phones={phone} if phone else set(),
                all_names={name} if name else set(),
                all_hashes={e_hash} if e_hash else set(),
            )
            self.identities_by_id[uid] = user
            if email:
                self.email_to_id[email.lower()] = uid

    def _load_audit_log(self):
        path = os.path.join(self.dataset_dir, "user_audit_log.csv")
        if not os.path.exists(path):
            return

        df = pd.read_csv(path)
        for _, row in df.iterrows():
            uid = str(row["user_id"]).strip()
            if uid not in self.identities_by_id:
                self.identities_by_id[uid] = UserIdentity(
                    user_id=uid,
                    current_email="",
                    current_phone="",
                    current_name="",
                    email_hash="",
                )
            user = self.identities_by_id[uid]
            field_name = str(row["field_name"]).strip().lower()

            for val in [row["old_value"], row["new_value"]]:
                if pd.notna(val):
                    sval = str(val).strip()
                    if not sval:
                        continue
                    if field_name == "email":
                        user.all_emails.add(sval.lower())
                        if sval.lower() not in self.email_to_id:
                            self.email_to_id[sval.lower()] = uid
                    elif field_name == "phone":
                        user.all_phones.add(sval)
                    elif field_name == "name":
                        user.all_names.add(sval)
                    elif field_name == "email_hash":
                        user.all_hashes.add(sval)

    def resolve_by_email(self, email: str) -> Optional[UserIdentity]:
        if not email:
            return None
        norm_email = email.strip().lower()
        uid = self.email_to_id.get(norm_email)
        if uid:
            return self.identities_by_id.get(uid)
        return None

    def resolve_by_user_id(self, user_id: str) -> Optional[UserIdentity]:
        if not user_id:
            return None
        return self.identities_by_id.get(user_id.strip())

    def matches_user(self, user: UserIdentity, text: str) -> bool:
        """Check if a string matches any of the user's identifiers."""
        if not text:
            return False
        text_lower = text.lower()
        if user.user_id.lower() in text_lower:
            return True
        for em in user.all_emails:
            if em in text_lower:
                return True
        for ph in user.all_phones:
            if ph in text:
                return True
        for h in user.all_hashes:
            if h in text:
                return True
        return False
