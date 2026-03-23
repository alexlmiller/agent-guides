#!/usr/bin/env python3
"""
Contact deduplication and unification service.

Reads VCF contacts from all Radicale collections, identifies duplicates
using multi-signal fuzzy matching, merges them, and fans out the unified
set to all collections.

Matching signals (in order of strength):
  - FullContact cluster ID match (X-FC-CLUSTER-ID)
  - Exact email match (normalized)
  - Exact phone match (normalized)
  - High name similarity + shared org
  - High name similarity + shared phone prefix

Designed to run as a scheduled job after vdirsyncer pulls.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# vobject for VCF parsing
import vobject

log = logging.getLogger("contact-dedup")


# ---------------------------------------------------------------------------
# Contact data model
# ---------------------------------------------------------------------------

@dataclass
class Contact:
    uid: str
    fn: str  # formatted name
    family_name: str
    given_name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)  # normalized
    phones_raw: list[str] = field(default_factory=list)
    org: str = ""
    title: str = ""
    fc_cluster_id: str = ""
    rev: str = ""  # last modified
    source_collection: str = ""
    source_path: str = ""
    raw_vcf: str = ""  # original VCF text

    @property
    def name_key(self) -> str:
        """Normalized name for comparison."""
        return f"{self.given_name} {self.family_name}".strip().lower()

    @property
    def family_key(self) -> str:
        return self.family_name.strip().lower()


def normalize_phone(phone: str) -> str:
    """Strip all non-digit chars, normalize to digits only."""
    digits = re.sub(r"[^\d]", "", phone)
    # Strip leading 1 for US numbers if 11 digits
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def normalize_email(email: str) -> str:
    return email.strip().lower()


def parse_vcf_file(path: Path, collection_name: str) -> Optional[Contact]:
    """Parse a single VCF file into a Contact."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        vcard = vobject.readOne(text)
    except Exception as e:
        log.warning("Failed to parse %s: %s", path, e)
        return None

    uid = getattr(vcard, "uid", None)
    uid = uid.value if uid else path.stem

    fn_obj = getattr(vcard, "fn", None)
    fn = fn_obj.value if fn_obj else ""

    n_obj = getattr(vcard, "n", None)
    family = n_obj.value.family if n_obj else ""
    given = n_obj.value.given if n_obj else ""

    emails = []
    for child in vcard.getChildren():
        if child.name.upper() == "EMAIL":
            emails.append(normalize_email(child.value))

    phones = []
    phones_raw = []
    for child in vcard.getChildren():
        if child.name.upper() == "TEL":
            phones_raw.append(child.value)
            phones.append(normalize_phone(child.value))

    org_obj = getattr(vcard, "org", None)
    org = ""
    if org_obj:
        org = org_obj.value[0] if isinstance(org_obj.value, list) else org_obj.value

    title_obj = getattr(vcard, "title", None)
    title = title_obj.value if title_obj else ""

    rev_obj = getattr(vcard, "rev", None)
    rev = rev_obj.value if rev_obj else ""

    fc_cluster = ""
    for child in vcard.getChildren():
        if child.name.upper() == "X-FC-CLUSTER-ID":
            fc_cluster = child.value
            break

    return Contact(
        uid=uid, fn=fn, family_name=family, given_name=given,
        emails=emails, phones=phones, phones_raw=phones_raw,
        org=org, title=title, fc_cluster_id=fc_cluster, rev=rev,
        source_collection=collection_name, source_path=str(path),
        raw_vcf=text,
    )


# ---------------------------------------------------------------------------
# Name similarity (Jaro-Winkler)
# ---------------------------------------------------------------------------

def jaro_similarity(s1: str, s2: str) -> float:
    """Jaro string similarity (0.0 to 1.0)."""
    if s1 == s2:
        return 1.0
    len_s1, len_s2 = len(s1), len(s2)
    if len_s1 == 0 or len_s2 == 0:
        return 0.0

    match_distance = max(len_s1, len_s2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len_s1
    s2_matches = [False] * len_s2
    matches = 0
    transpositions = 0

    for i in range(len_s1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len_s2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len_s1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (matches / len_s1 + matches / len_s2 +
            (matches - transpositions / 2) / matches) / 3
    return jaro


def jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    """Jaro-Winkler similarity with prefix boost."""
    jaro = jaro_similarity(s1, s2)
    prefix_len = 0
    for i in range(min(4, len(s1), len(s2))):
        if s1[i] == s2[i]:
            prefix_len += 1
        else:
            break
    return jaro + prefix_len * p * (1 - jaro)


def name_similarity(c1: Contact, c2: Contact) -> float:
    """Compare two contacts by name, handling common variations."""
    n1 = c1.name_key
    n2 = c2.name_key
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0

    # Try full name comparison
    full_sim = jaro_winkler(n1, n2)

    # Also compare family names separately (more weight)
    fam_sim = jaro_winkler(c1.family_key, c2.family_key) if c1.family_key and c2.family_key else 0.0

    # Given name comparison
    g1, g2 = c1.given_name.lower(), c2.given_name.lower()
    given_sim = jaro_winkler(g1, g2) if g1 and g2 else 0.0

    # Check if one given name is a prefix of the other (e.g., "Alex" vs "Alexander")
    prefix_bonus = 0.0
    if g1 and g2:
        shorter, longer = (g1, g2) if len(g1) <= len(g2) else (g2, g1)
        if longer.startswith(shorter) and len(shorter) >= 3:
            prefix_bonus = 0.15

    # Weighted combination: family name matters most
    combined = fam_sim * 0.5 + given_sim * 0.3 + full_sim * 0.2 + prefix_bonus
    return min(combined, 1.0)


# ---------------------------------------------------------------------------
# Pair scoring
# ---------------------------------------------------------------------------

def score_pair(c1: Contact, c2: Contact) -> float:
    """Score how likely two contacts are the same person (0.0 to 1.0).

    Key principle: no single signal is sufficient except shared email
    (which is almost always unique to a person). All other signals need
    corroboration, especially name similarity, to avoid merging coworkers
    who share an office phone or contacts in the same FullContact cluster
    by mistake.
    """
    # Same UID across collections = the same contact fanned out, not a dupe
    if c1.uid == c2.uid:
        return 0.0

    both_named = bool(c1.name_key and c2.name_key)
    neither_named = not c1.name_key and not c2.name_key
    nsim = name_similarity(c1, c2) if both_named else 0.0

    # Signal 1: Shared email — strongest, almost always unique to a person
    shared_emails = set(c1.emails) & set(c2.emails)
    if shared_emails:
        if neither_named:
            # Both nameless, same email — same stub/business contact
            return 0.9
        if not both_named:
            # One side has no name — assume it's a stub of the same contact
            return 0.9
        # Check given names specifically — catches family members sharing
        # an email (e.g., mike.kathy.tess@gmail.com).
        g1, g2 = c1.given_name.lower().strip(), c2.given_name.lower().strip()
        if g1 and g2:
            given_sim = jaro_winkler(g1, g2)
            if given_sim < 0.6:
                # Clearly different first names sharing an email → family/couple
                return 0.0
        # Same email, compatible names — same person
        return 0.95

    # Signal 2: Org-only contacts (businesses) — match by org name + phone
    # Restaurants, pharmacies, etc. have org but no person name.
    if neither_named and c1.org and c2.org:
        org_sim = jaro_winkler(c1.org.lower(), c2.org.lower())
        shared_phones = set(c1.phones) & set(c2.phones)
        if org_sim > 0.9 and shared_phones:
            # Same business name + same phone = same business
            return 0.92
        if org_sim > 0.95:
            # Near-exact org name match even without phone (e.g., no phone on either)
            return 0.85
        return 0.0

    # From here on, we need both contacts to have names
    if not both_named:
        return 0.0

    # Signal 3: FullContact cluster ID — strong but needs name corroboration
    # FC sometimes clusters coworkers or family members
    if c1.fc_cluster_id and c2.fc_cluster_id and c1.fc_cluster_id == c2.fc_cluster_id:
        if nsim > 0.6:
            return 0.92
        return 0.0

    # Signal 4: Shared phone — NOT sufficient alone (office phones exist)
    # Must be corroborated by name similarity
    shared_phones = set(c1.phones) & set(c2.phones)
    if shared_phones:
        if nsim > 0.7:
            return 0.85
        # Shared phone but different names → coworkers, not same person
        return 0.0

    # No hard identifier overlap → don't merge.
    # Name similarity alone is never sufficient.
    return 0.0


# ---------------------------------------------------------------------------
# Clustering (union-find)
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px != py:
            self.parent[px] = py

    def groups(self) -> dict[str, list[str]]:
        result = defaultdict(list)
        for x in self.parent:
            result[self.find(x)].append(x)
        return dict(result)


# ---------------------------------------------------------------------------
# Blocking (reduce O(n²) comparisons)
# ---------------------------------------------------------------------------

def build_blocks(contacts: list[Contact]) -> dict[str, list[int]]:
    """Group contacts into blocks that should be compared.

    Uses multiple blocking keys so the same contact can appear in
    multiple blocks (ensuring we don't miss matches).
    """
    blocks = defaultdict(list)
    for i, c in enumerate(contacts):
        # Block by family name first 3 chars
        if c.family_key and len(c.family_key) >= 2:
            blocks[f"fam:{c.family_key[:3]}"].append(i)

        # Block by email domain
        for email in c.emails:
            parts = email.split("@")
            if len(parts) == 2:
                blocks[f"edom:{parts[1]}"].append(i)

        # Block by exact phone (not prefix — avoids matching coworkers)
        for phone in c.phones:
            if len(phone) >= 7:
                blocks[f"ph:{phone}"].append(i)

        # Block by org name for nameless contacts (businesses)
        if not c.name_key and c.org:
            org_key = c.org.strip().lower()[:20]
            blocks[f"org:{org_key}"].append(i)

        # Block by FullContact cluster ID
        if c.fc_cluster_id:
            blocks[f"fc:{c.fc_cluster_id}"].append(i)

        # Block by exact email (for cross-collection matching)
        for email in c.emails:
            blocks[f"em:{email}"].append(i)

    return blocks


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge_contacts(cluster: list[Contact]) -> str:
    """Merge a cluster of duplicate contacts into a single canonical VCF.

    Strategy: union all fields, prefer most recently modified for single-value fields.
    """
    # Sort by rev date (most recent first) for single-value field preference
    def rev_sort_key(c):
        try:
            # Handle both formats: "20230516T215532Z" and "2025-10-16T20:54:40Z"
            rev = c.rev.replace("-", "").replace(":", "")
            return rev
        except Exception:
            return ""

    sorted_contacts = sorted(cluster, key=rev_sort_key, reverse=True)
    primary = sorted_contacts[0]  # most recently modified

    # Start with the primary's VCF and enrich it
    try:
        vcard = vobject.readOne(primary.raw_vcf)
    except Exception:
        return primary.raw_vcf  # fallback to raw if parse fails

    # Collect all unique emails, phones, orgs
    all_emails = set()
    all_phones = {}  # normalized -> raw
    all_orgs = set()
    all_titles = set()

    for c in cluster:
        for e in c.emails:
            all_emails.add(e)
        for norm, raw in zip(c.phones, c.phones_raw):
            if norm not in all_phones:
                all_phones[norm] = raw
        if c.org:
            all_orgs.add(c.org)
        if c.title:
            all_titles.add(c.title)

    # Remove existing EMAIL and TEL entries
    children_to_remove = []
    for child in vcard.getChildren():
        if child.name.upper() in ("EMAIL", "TEL"):
            children_to_remove.append(child)
    for child in children_to_remove:
        vcard.remove(child)

    # Add all unique emails
    for i, email in enumerate(sorted(all_emails)):
        e = vcard.add("email")
        e.value = email
        if i == 0:
            e.type_param = "PREF"

    # Add all unique phones
    for i, (norm, raw) in enumerate(sorted(all_phones.items())):
        t = vcard.add("tel")
        t.value = raw
        if i == 0:
            t.type_param = "PREF"

    # Use best org/title — prefer primary, fall back to any cluster member
    best_org = primary.org or next((c.org for c in sorted_contacts if c.org), "")
    best_title = primary.title or next((c.title for c in sorted_contacts if c.title), "")
    if best_org:
        # Remove existing org to avoid duplicates
        for child in list(vcard.getChildren()):
            if child.name.upper() == "ORG":
                vcard.remove(child)
        vcard.add("org").value = [best_org]
    if best_title:
        for child in list(vcard.getChildren()):
            if child.name.upper() == "TITLE":
                vcard.remove(child)
        vcard.add("title").value = best_title

    # Update REV to now
    if hasattr(vcard, "rev"):
        vcard.rev.value = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        vcard.add("rev").value = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Add provenance: track which UIDs were merged
    source_uids = [c.uid for c in cluster if c.uid != primary.uid]
    if source_uids:
        note = vcard.add("note")
        note.value = f"Merged from: {', '.join(source_uids)}"

    return vcard.serialize()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def scan_collections(base_path: Path) -> dict[str, list[Contact]]:
    """Scan Radicale collection root for all address books."""
    collections = {}
    collection_root = base_path / "collection-root"
    if not collection_root.exists():
        log.error("Collection root not found: %s", collection_root)
        return collections

    # Walk to find directories containing .vcf files
    for dirpath in collection_root.rglob("*"):
        if not dirpath.is_dir():
            continue
        vcf_files = list(dirpath.glob("*.vcf"))
        if not vcf_files:
            continue
        # Skip .Radicale.cache directories
        if ".Radicale.cache" in str(dirpath):
            continue

        collection_name = str(dirpath.relative_to(collection_root))
        contacts = []
        for vcf_path in vcf_files:
            c = parse_vcf_file(vcf_path, collection_name)
            if c:
                contacts.append(c)
        if contacts:
            collections[collection_name] = contacts
            log.info("Collection '%s': %d contacts", collection_name, len(contacts))

    return collections


def find_duplicates(all_contacts: list[Contact], threshold: float = 0.7) -> list[list[Contact]]:
    """Find duplicate clusters using blocking + pair scoring."""
    blocks = build_blocks(all_contacts)
    uf = UnionFind()
    comparisons = 0
    matches = 0

    # Compare within each block
    seen_pairs = set()
    for block_key, indices in blocks.items():
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                idx_a, idx_b = indices[i], indices[j]
                pair = (min(idx_a, idx_b), max(idx_a, idx_b))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                comparisons += 1

                score = score_pair(all_contacts[idx_a], all_contacts[idx_b])
                if score >= threshold:
                    uf.union(str(idx_a), str(idx_b))
                    matches += 1
                    log.debug(
                        "Match (%.2f): '%s' [%s] <-> '%s' [%s]",
                        score,
                        all_contacts[idx_a].fn, all_contacts[idx_a].source_collection,
                        all_contacts[idx_b].fn, all_contacts[idx_b].source_collection,
                    )

    log.info("Comparisons: %d, Matches: %d", comparisons, matches)

    # Build candidate clusters (only groups with >1 contact)
    raw_clusters = []
    for group_indices in uf.groups().values():
        if len(group_indices) > 1:
            cluster = [all_contacts[int(i)] for i in group_indices]
            raw_clusters.append(cluster)

    # Validate clusters: ensure all members match the primary directly.
    # This breaks apart bad transitive chains like A-B-C where A doesn't
    # actually match C.
    clusters = []
    for cluster in raw_clusters:
        validated = validate_cluster(cluster, threshold)
        clusters.extend(validated)

    return clusters


def validate_cluster(cluster: list[Contact], threshold: float) -> list[list[Contact]]:
    """Break a cluster into valid sub-clusters.

    Pick the contact with the most data as the anchor, then only keep
    contacts that directly match the anchor above threshold.
    Also verify that all named contacts in the cluster have compatible
    names — prevents family members (James/Zem Joaquin) from being
    merged through unnamed stubs.
    Recurse on the remainder.
    """
    if len(cluster) <= 1:
        return [cluster] if len(cluster) == 1 else []
    if len(cluster) == 2:
        return [cluster]  # already validated by pair scoring

    # Pre-check: if cluster has multiple named contacts with incompatible
    # given names, split them BEFORE anchor selection. This catches the
    # family-bridge pattern (unnamed stubs linking different people).
    named = [c for c in cluster if c.name_key]
    if len(named) >= 2:
        # Group named contacts by compatible given names
        name_groups = []
        for c in named:
            placed = False
            for group in name_groups:
                rep = group[0]
                g1 = c.given_name.lower().strip()
                g2 = rep.given_name.lower().strip()
                if g1 and g2 and jaro_winkler(g1, g2) < 0.6:
                    continue  # incompatible given names
                group.append(c)
                placed = True
                break
            if not placed:
                name_groups.append([c])

        # If we found multiple incompatible name groups, split the cluster
        if len(name_groups) > 1:
            unnamed = [c for c in cluster if not c.name_key]
            result = []
            for group in name_groups:
                # Assign unnamed contacts to the group they share emails with
                sub = list(group)
                for u in unnamed:
                    for g in group:
                        if set(u.emails) & set(g.emails):
                            sub.append(u)
                            break
                if len(sub) > 1:
                    result.extend(validate_cluster(sub, threshold))
                elif len(sub) == 1:
                    pass  # single contact, not a cluster
            return result

    # Pick anchor: prefer named contacts with most fields populated
    def richness(c):
        return (len(c.name_key) > 0, len(c.emails), len(c.phones), c.rev or "")
    anchor = max(cluster, key=richness)

    # Keep only contacts that match anchor directly
    matched = [anchor]
    unmatched = []
    for c in cluster:
        if c.uid == anchor.uid:
            continue
        if score_pair(anchor, c) >= threshold:
            matched.append(c)
        else:
            unmatched.append(c)

    result = []
    if len(matched) > 1:
        result.append(matched)

    # Recurse on unmatched to find sub-clusters
    if len(unmatched) > 1:
        result.extend(validate_cluster(unmatched, threshold))

    return result


def run_dedup(
    radicale_data: str,
    threshold: float = 0.7,
    dry_run: bool = True,
):
    """Main dedup pipeline."""
    base = Path(radicale_data) / "collections"
    if not base.exists():
        log.error("Radicale data path not found: %s", base)
        sys.exit(1)

    # 1. Scan all collections
    collections = scan_collections(base)
    all_contacts = []
    for name, contacts in collections.items():
        all_contacts.extend(contacts)
    log.info("Total contacts across all collections: %d", len(all_contacts))

    if not all_contacts:
        log.info("No contacts found, nothing to do.")
        return

    # 2. Find duplicate clusters
    clusters = find_duplicates(all_contacts, threshold)
    log.info("Found %d duplicate clusters", len(clusters))

    if not clusters:
        log.info("No duplicates found.")
        if dry_run:
            return
        # Still need to run unification pass (step 6) even with no dupes
        clusters = []

    # 3. Report
    cross_collection = 0
    same_collection = 0
    total_mergeable = 0
    for cluster in clusters:
        sources = set(c.source_collection for c in cluster)
        if len(sources) > 1:
            cross_collection += 1
        else:
            same_collection += 1
        total_mergeable += len(cluster) - 1  # contacts that would be removed

    log.info("Cross-collection duplicates: %d", cross_collection)
    log.info("Same-collection duplicates: %d", same_collection)
    log.info("Total contacts that would be merged away: %d", total_mergeable)

    # 4. Show top duplicates
    for i, cluster in enumerate(sorted(clusters, key=len, reverse=True)[:20]):
        names = [f"'{c.fn}' [{c.source_collection}]" for c in cluster]
        log.info("  Cluster %d (%d contacts): %s", i + 1, len(cluster), " | ".join(names))

    if dry_run:
        log.info("DRY RUN — no changes made. Use --apply to write changes.")
        # Write report to stdout as JSON
        report = {
            "total_contacts": len(all_contacts),
            "collections": {name: len(contacts) for name, contacts in collections.items()},
            "duplicate_clusters": len(clusters),
            "cross_collection_dupes": cross_collection,
            "same_collection_dupes": same_collection,
            "mergeable_contacts": total_mergeable,
            "sample_clusters": [
                {
                    "size": len(cluster),
                    "contacts": [
                        {"fn": c.fn, "emails": c.emails, "phones": c.phones_raw,
                         "org": c.org, "source": c.source_collection}
                        for c in cluster
                    ]
                }
                for cluster in sorted(clusters, key=len, reverse=True)[:50]
            ],
        }
        print(json.dumps(report, indent=2))
        return

    # 5. Apply merges
    all_collection_names = list(collections.keys())
    collection_root = base / "collection-root"
    merged_count = 0

    # Use the same rev_sort_key as merge_contacts for consistent primary selection
    def rev_sort_key(c):
        try:
            return c.rev.replace("-", "").replace(":", "")
        except Exception:
            return ""

    for cluster in clusters:
        # Merge the cluster
        merged_vcf = merge_contacts(cluster)
        primary = sorted(cluster, key=rev_sort_key, reverse=True)[0]

        # Write merged contact to primary's location
        primary_path = Path(primary.source_path)
        primary_path.write_text(merged_vcf, encoding="utf-8")
        log.info("Updated primary: %s (%s)", primary.fn, primary_path.name)

        # Delete non-primary duplicates from their source collections
        for c in cluster:
            if c.uid != primary.uid:
                dup_path = Path(c.source_path)
                if dup_path.exists():
                    dup_path.unlink()
                    log.info("  Removed duplicate: %s (%s) from %s",
                             c.fn, dup_path.name, c.source_collection)

        # Fan out to ALL collections (full unification)
        for coll_name in all_collection_names:
            if coll_name == primary.source_collection:
                continue  # already written above
            target_dir = collection_root / coll_name
            target_path = target_dir / f"{primary.uid}.vcf"
            if not target_path.exists():
                target_path.write_text(merged_vcf, encoding="utf-8")
                log.info("  Fanned out to: %s/%s", coll_name, target_path.name)

        merged_count += 1

    # 6. Full unification pass — fan out ALL contacts to ALL collections
    # (not just merged ones — contacts that were never duplicated also
    # need to appear in every collection for true unification)
    fanout_count = 0
    all_uids_by_collection = defaultdict(set)
    for coll_name, contacts in collections.items():
        for c in contacts:
            all_uids_by_collection[coll_name].add(c.uid)

    # Re-scan after merges to get current state
    current_collections = scan_collections(base)
    uid_to_vcf = {}  # uid -> (path, collection)
    for coll_name, contacts in current_collections.items():
        for c in contacts:
            if c.uid not in uid_to_vcf:
                uid_to_vcf[c.uid] = (c.source_path, coll_name)

    for uid, (source_path, source_coll) in uid_to_vcf.items():
        for coll_name in current_collections:
            if coll_name == source_coll:
                continue
            target_dir = collection_root / coll_name
            target_path = target_dir / f"{uid}.vcf"
            if not target_path.exists():
                vcf_text = Path(source_path).read_text(encoding="utf-8")
                target_path.write_text(vcf_text, encoding="utf-8")
                fanout_count += 1

    if fanout_count:
        log.info("Unified %d contacts across all collections.", fanout_count)

    log.info("Merged %d duplicate clusters. Done.", merged_count)


def main():
    parser = argparse.ArgumentParser(description="Contact deduplication service")
    parser.add_argument(
        "--data", default="/data",
        help="Path to Radicale data directory (default: /data)",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.7,
        help="Minimum score to consider contacts as duplicates (default: 0.7)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually apply merges (default is dry-run)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    run_dedup(
        radicale_data=args.data,
        threshold=args.threshold,
        dry_run=not args.apply,
    )


if __name__ == "__main__":
    main()
