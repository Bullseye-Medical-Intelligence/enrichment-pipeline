"""
tests/test_consolidator.py
Practice-location consolidation: Pass 1 (merge) and Pass 2 (link, never merge).

Deterministic and hermetic — pure functions over dict fixtures, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingestion.consolidator import (  # noqa: E402
    MAX_CONTACT_BLOCK,
    MERGE_THRESHOLD,
    NOISE_DOMAINS,
    domain_policy,
    UMBRELLA_DOMAINS,
    consolidate_records,
    link_location_groups,
    score_pair,
    units_conflict,
)
from ingestion.practice_normalizer import identity_of  # noqa: E402


def _row(rid, name, street="123 Main St", unit="", zip_="95823",
         phone="916-555-0100", website="https://valleyclinic.com", **over):
    record = {
        "id": rid,
        "practice_name": name,
        "provider_names": [],
        "specialty": "OBGYN",
        "npi_optional": None,
        "website_url": website,
        "phone": phone,
        "address_street": street,
        "address_unit": unit,
        "address_city": "Sacramento",
        "address_state": "CA",
        "address_zip": zip_,
    }
    record.update(over)
    return record


def _by_name(records):
    return {r["practice_name"]: r for r in records}


# ---------------------------------------------------------------------------
# The unit gate — a hard stop no score may override
# ---------------------------------------------------------------------------

class TestUnitGate:

    def test_differing_units_never_merge_despite_perfect_score(self):
        """Same street, ZIP, phone, domain and name — but Suite 200 vs Suite 400.
        These are two practices in one building and must stay separate."""
        rows = [
            _row("T-1", "Valley Clinic", unit="Suite 200"),
            _row("T-2", "Valley Clinic", unit="Suite 400"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["merged_groups"] == 0

    def test_a_bare_or_rule_would_have_merged_these(self):
        """Domain and name both match, so any OR-of-signals rule merges them.
        The unit gate is what prevents it."""
        left = identity_of(_row("T-1", "Valley Clinic", unit="Suite 200"))
        right = identity_of(_row("T-2", "Valley Clinic", unit="Suite 400"))
        score, _ = score_pair(left, right, frozenset())
        assert score >= 6                      # scoring alone says merge
        assert units_conflict(left, right)     # the gate says no

    def test_unit_versus_no_unit_may_merge(self):
        """Only two PRESENT and differing units are a conflict; a missing unit
        is missing data, not evidence of a different suite."""
        rows = [
            _row("T-1", "Valley Clinic", unit="Suite 200"),
            _row("T-2", "Valley Clinic", unit=""),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1

    def test_unit_conflict_cannot_sneak_in_through_transitivity(self):
        """Suite 200 and Suite 400 both merge with a unit-less row. The cluster
        must never end up holding both suites."""
        rows = [
            _row("T-1", "Valley Clinic", unit="Suite 200"),
            _row("T-2", "Valley Clinic", unit=""),
            _row("T-3", "Valley Clinic", unit="Suite 400"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        # The two suites end up in different records, and the unit-less row
        # joins exactly one of them.
        assert sorted(r["address_unit"] for r in out) == ["Suite 200", "Suite 400"]
        by_unit = {r["address_unit"]: r for r in out}
        assert by_unit["Suite 200"]["source_row_ids"] == ["T-1", "T-2"]
        assert by_unit["Suite 400"]["source_row_ids"] == ["T-3"]
        # The merge the gate refused is surfaced for review, never dropped.
        assert summary["review_pairs"] == 1
        assert by_unit["Suite 400"]["consolidation"]["review_candidates"]

    def test_unit_in_the_street_line_is_honoured(self):
        """The suite arrives inside the street string, not its own column."""
        rows = [
            _row("T-1", "Valley Clinic", street="123 Main St Ste 200", unit=""),
            _row("T-2", "Valley Clinic", street="123 Main St Ste 400", unit=""),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Pass 1 scoring
# ---------------------------------------------------------------------------

class TestPass1Scoring:

    def test_department_phone_numbers_still_merge(self):
        """The case a conjunctive 'same address AND same phone' rule fails:
        one practice scraped with a main line and a scheduling line. Address (4)
        plus domain (3) reaches 7 and merges even though the phones differ."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999"),
        ]
        left, right = identity_of(rows[0]), identity_of(rows[1])
        score, matched = score_pair(left, right, frozenset())
        assert score == 7
        assert matched == ["address", "domain"]     # phone and name did not fire
        out, summary = consolidate_records(rows, {})
        assert len(out) == 1
        assert summary["merged_groups"] == 1

    def test_sharing_only_a_building_is_kept_apart_without_a_question(self):
        """Score 4 on the address alone: two tenants of one building.

        Measured on real lists, ~91-98% of address-only pairs are unrelated
        neighbours. They stay separate records, and no analyst is asked.
        """
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100", website=""),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999", website=""),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 4 and matched == ["address"]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2                       # still never merged
        assert summary["review_pairs"] == 0        # and never queued
        for record in out:
            assert record["consolidation"]["review_candidates"] == []


class TestReviewQueueCounting:
    """The queue's unit is a location pair — one analyst decision — not a row edge."""

    def _two_clusters_one_question(self):
        """Two names in one building, each built from two rows, no phone anywhere.

        Every A-row scores 4 (address only) against every B-row, so four row-level
        edges describe a single question: is Valley the same location as Harbor?
        The absent phone is what admits it — differing phones would not.
        """
        return [
            _row("T-1", "Valley Clinic", phone="", website=""),
            _row("T-2", "Valley Clinic", phone="", website=""),
            _row("T-3", "Harbor Health", phone="", website=""),
            _row("T-4", "Harbor Health", phone="", website=""),
        ]

    def test_counts_distinct_location_pairs_not_row_edges(self):
        """Counting edges overstated a real list 4x (2,415 edges vs 607 pairs)."""
        out, summary = consolidate_records(self._two_clusters_one_question(), {})
        assert len(out) == 2
        assert summary["review_pairs"] == 1

    def test_each_side_records_the_other_once(self):
        """The engine count must agree with what the review page renders."""
        out, summary = consolidate_records(self._two_clusters_one_question(), {})
        for record in out:
            assert len(record["consolidation"]["review_candidates"]) == 1
        rendered = {
            tuple(sorted((r["practice_id"], c["practice_id"])))
            for r in out for c in r["consolidation"]["review_candidates"]
        }
        assert len(rendered) == summary["review_pairs"]

    def test_engine_count_matches_rendered_pairs_on_a_wider_set(self):
        """Three co-located practices: three questions, not nine edges."""
        rows = self._two_clusters_one_question() + [
            _row("T-5", "Delta Medical", phone="", website=""),
            _row("T-6", "Delta Medical", phone="", website=""),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 3
        rendered = {
            tuple(sorted((r["practice_id"], c["practice_id"])))
            for r in out for c in r["consolidation"]["review_candidates"]
        }
        assert summary["review_pairs"] == len(rendered) == 3


class TestUnitMatchScoring:
    """One suite is one front door, so two providers in it are one location.

    Settled by ruling thirteen sampled same-suite decisions, all thirteen MERGE,
    including three independent single-physician practices whose only
    corroboration was a shared area code. The standard is not "same legal entity"
    but "would a rep knock once or twice".
    """

    def _pair(self, left=None, right=None):
        left = {"unit": "", "phone": "916-555-0100", "website": "", **(left or {})}
        right = {"unit": "", "phone": "916-555-0999", "website": "", **(right or {})}
        rows = [_row("T-1", "Alpha Health", **left),
                _row("T-2", "Zeta Partners", **right)]
        return consolidate_records(rows, {})

    def test_address_plus_suite_reaches_the_merge_threshold(self):
        left, right = identity_of(_row("T-1", "Alpha", unit="Suite 360")), \
            identity_of(_row("T-2", "Zeta", unit="Suite 360", phone="916-555-0999"))
        score, matched = score_pair(left, right, frozenset())
        assert score >= MERGE_THRESHOLD
        assert "unit" in matched

    def test_two_providers_at_one_suite_merge(self):
        out, summary = self._pair(left={"unit": "Suite 360"},
                                  right={"unit": "Suite 360"})
        assert len(out) == 1
        assert summary["review_pairs"] == 0          # merged, never queued
        assert out[0]["provider_count"] >= 1

    def test_the_merge_survives_a_different_spelling_of_the_suite(self):
        out, _ = self._pair(left={"unit": "Suite 360"}, right={"unit": "STE #360"})
        assert len(out) == 1

    def test_a_suite_alone_does_not_merge_across_buildings(self):
        """Two "suite 200"s in different buildings are unrelated."""
        rows = [_row("T-1", "Alpha Health", street="1 A St", zip_="95821",
                     unit="Suite 200", phone="916-555-0100", website=""),
                _row("T-2", "Zeta Partners", street="2 B St", zip_="95822",
                     unit="Suite 200", phone="916-555-0999", website="")]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_a_conflicting_domain_holds_a_same_suite_pair_back_for_review(self):
        """Two real and different websites at one suite go to review, mechanically.

        4 + 3 - 3 = 4: under the merge threshold, inside the review band, and
        admitted as corroborated rather than silently merged or silently split.

        This is NOT the one-door standard hesitating — that standard says merge.
        It is the schema: a record carries exactly one website_url, so merging
        would force the engine to pick one site and discard the other's evidence.
        Backlog 22 (multiple URLs per location) is what would let it merge.
        """
        out, summary = self._pair(
            left={"unit": "Suite 360", "website": "https://alpha.com"},
            right={"unit": "Suite 360", "website": "https://zeta.com"})
        assert len(out) == 2
        assert summary["review_reasons"] == {"corroborated": 1}


class TestReviewAdmission:
    """Sharing a building is not a question. Sharing a suite is not one either."""

    def _pair(self, left=None, right=None):
        left = {"unit": "", "phone": "916-555-0100", "website": "", **(left or {})}
        right = {"unit": "", "phone": "916-555-0999", "website": "", **(right or {})}
        rows = [_row("T-1", "Alpha Health", **left),
                _row("T-2", "Zeta Partners", **right)]
        return consolidate_records(rows, {})

    def test_building_only_is_not_admitted(self):
        out, summary = self._pair()
        assert len(out) == 2 and summary["review_pairs"] == 0

    def test_same_suite_is_no_longer_a_review_reason(self):
        _, summary = self._pair(left={"unit": "Suite 360"},
                                right={"unit": "Suite 360"})
        assert "same_unit" not in summary["review_reasons"]

    def test_different_suites_are_not_admitted(self):
        """Differing units never reach scoring at all — the pairwise gate stops them."""
        out, summary = self._pair(left={"unit": "Suite 360"},
                                  right={"unit": "Suite 400"})
        assert len(out) == 2 and summary["review_pairs"] == 0

    def test_absent_phone_is_admitted(self):
        """An absent phone is no evidence of anything, so the pair is unknown."""
        _, summary = self._pair(left={"phone": ""})
        assert summary["review_reasons"] == {"phone_absent": 1}

    def test_differing_phones_alone_are_not_admitted(self):
        """A differing phone is weak evidence of difference, not a question."""
        _, summary = self._pair()
        assert summary["review_pairs"] == 0

    def test_corroborating_field_is_admitted(self):
        """Same address and same phone, conflicting domains: a real judgement call."""
        rows = [
            _row("T-1", "Dignity Urgent Care", phone="916-555-0100",
                 website="https://commonspirit.org"),
            _row("T-2", "Mercy Neurosurgery", phone="916-555-0100",
                 website="https://dignityhealth.org"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["review_reasons"] == {"corroborated": 1}

    def test_unit_gate_blocks_are_labelled_separately(self):
        """These scored a merge and were stopped by a hard veto — mechanical,
        not a judgement call, so they must not be filed as near-matches."""
        rows = [
            _row("T-1", "Valley Clinic", unit="Suite 200"),
            _row("T-2", "Valley Clinic", unit=""),
            _row("T-3", "Valley Clinic", unit="Suite 400"),
        ]
        _, summary = consolidate_records(rows, {})
        assert summary["review_reasons"] == {"unit_gate_block": 1}


class TestReviewEvidence:
    """A ruling is only useful later if what the engine saw is recorded with it."""

    def _candidate(self, rows):
        out, _ = consolidate_records(rows, {})
        return out[0]["consolidation"]["review_candidates"][0]

    def test_conflicting_domain_pair_records_its_evidence(self):
        candidate = self._candidate([
            _row("T-1", "Alpha Health", phone="916-555-0100",
                 website="https://alpha.com"),
            _row("T-2", "Zeta Partners", phone="916-555-0100",
                 website="https://zeta.com"),
        ])
        evidence = candidate["evidence"]
        assert candidate["review_reason"] == "corroborated"
        assert evidence["domains_conflict"] is True
        assert evidence["domain_left"] and evidence["domain_right"]
        assert evidence["phones_differ"] is False
        assert evidence["phone_absent"] is False

    def test_personal_versus_organizational_names_are_recorded(self):
        candidate = self._candidate([
            _row("T-1", "Jane Smith", phone="", website=""),
            _row("T-2", "John Doe", phone="916-555-0999", website=""),
        ])
        assert candidate["evidence"]["both_personal"] is True
        assert candidate["evidence"]["both_organizational"] is False
        assert candidate["evidence"]["phone_absent"] is True

    def test_cluster_sizes_are_recorded(self):
        candidate = self._candidate([
            _row("T-1", "Alpha Health", phone="", website=""),
            _row("T-2", "Alpha Health", phone="", website=""),
            _row("T-3", "Zeta Partners", phone="916-555-0999", website=""),
        ])
        sizes = {candidate["evidence"]["rows_left"], candidate["evidence"]["rows_right"]}
        assert sizes == {1, 2}

    def test_a_noise_domain_is_not_reported_as_a_conflict(self):
        candidate = self._candidate([
            _row("T-1", "Alpha Health", phone="",
                 website="https://www.facebook.com/alpha"),
            _row("T-2", "Zeta Partners", phone="916-555-0999",
                 website="https://zeta.com"),
        ])
        assert candidate["evidence"]["domains_conflict"] is False


class TestDomainConflictPenalty:
    """Agreement alone cannot tell "no data" from "contradicting data" — both
    land on the bare address score. The conflict penalty separates them."""

    def test_two_practices_one_building_each_with_its_own_site_are_separate(self):
        """The medical-office-building case: 4 - 3 = 1, below review."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://alpha-health.com"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://zeta-fertility.com"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 1
        assert matched == ["address", "domain_conflict"]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["review_pairs"] == 0        # never entered the queue

    def test_shared_site_still_merges(self):
        """6 providers, one practice, one shared site: 4 + 3 = 7."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999"),
        ]
        score, _ = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 7
        assert len(consolidate_records(rows, {})[0]) == 1

    def test_no_site_but_matching_org_name_still_merges(self):
        """6 providers, one practice, no site, org name matches: 4 + 2 = 6."""
        rows = [
            _row("T-1", "Valley Womens Health", website="", phone="916-555-0100"),
            _row("T-2", "Valley Women's Health", website="", phone="916-555-0999"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 6 and matched == ["address", "name"]
        assert len(consolidate_records(rows, {})[0]) == 1

    def test_differing_phones_are_never_penalised(self):
        """Absence of a phone match is not evidence of difference — one practice
        publishes a main line, a scheduling line and a billing line. Penalising
        that would break the case consolidation exists to fix."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100"),
            _row("T-2", "Alpha Womens Health", phone="916-555-0999"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert "phone_conflict" not in matched
        assert score == 9                       # address 4 + domain 3 + name 2
        assert len(consolidate_records(rows, {})[0]) == 1

    def test_one_missing_domain_is_not_a_conflict(self):
        """A row with no website contradicts nothing."""
        rows = [
            _row("T-1", "Alpha Womens Health", website="https://alpha-health.com",
                 phone="916-555-0100"),
            _row("T-2", "Zeta Fertility Partners", website="", phone="916-555-0999"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 4 and matched == ["address"]

    def test_two_different_directory_listings_are_not_a_conflict(self):
        """Denylisted hosts prove nothing in either direction, so two different
        directory URLs must not be read as two practices."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://www.healthgrades.com/dr-alpha"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://www.zocdoc.com/dr-zeta"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]),
                                    NOISE_DOMAINS)
        assert score == 4 and matched == ["address"]

    def test_reachable_scores_inside_a_block(self):
        """Blocking is on (zip5 + street), so every in-block pair already carries
        the +4 address component. With the conflict penalty the reachable sums
        are 1, 3, 4, 6, 7, 9, 10 and 12 — 5 and 8 remain unreachable, so a
        review band defined as "5 only" would abolish the queue."""
        components = {4}                       # address always present in a block
        reachable = set()
        for phone in (0, 3):
            for domain in (0, 3, -3):
                for name in (0, 2):
                    reachable.add(4 + phone + domain + name)
        assert 5 not in reachable and 8 not in reachable
        assert reachable == {1, 3, 4, 6, 7, 9, 10, 12}
        assert components <= reachable

    def test_phone_match_lifts_review_to_merge(self):
        """Same address, same phone, neither row has a site: 4 + 3 = 7."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100", website=""),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0100", website=""),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 7 and matched == ["address", "phone"]
        assert len(consolidate_records(rows, {})[0]) == 1

    def test_shared_phone_but_conflicting_sites_lands_in_review(self):
        """Address and phone agree, but two real and different websites disagree:
        4 + 3 - 3 = 4. Genuinely ambiguous, so it goes to a human rather than
        being merged or silently split."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://alpha-health.com"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0100",
                 website="https://zeta-fertility.com"),
        ]
        score, _ = score_pair(identity_of(rows[0]), identity_of(rows[1]), frozenset())
        assert score == 4
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2 and summary["review_pairs"] == 1

    def test_name_similarity_scores_two(self):
        left = identity_of(_row("T-1", "Valley Womens Health", website="",
                                phone="916-555-0100"))
        right = identity_of(_row("T-2", "Valley Women's Health", website="",
                                 phone="916-555-0999"))
        score, matched = score_pair(left, right, frozenset())
        assert "name" in matched and score == 6      # address 4 + name 2

    def test_different_addresses_with_own_contacts_are_never_compared(self):
        """Address blocking is (zip5 + street), so two locations at different
        addresses share no key. With their own phones there is no contact key
        either, and they stay separate records — Pass 2 links them instead."""
        rows = [
            _row("T-1", "Valley Clinic", street="123 Main St", phone="916-555-0100"),
            _row("T-2", "Valley Clinic", street="900 Oak Ave", phone="916-555-0200"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["merged_groups"] == 0
        assert summary["cross_address_merges"] == 0


# ---------------------------------------------------------------------------
# Aggregator denylist
# ---------------------------------------------------------------------------

class TestAggregatorDenylist:

    def test_denylisted_domain_contributes_zero(self):
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://www.healthgrades.com/dr-alpha"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://www.healthgrades.com/dr-zeta"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]),
                                    NOISE_DOMAINS)
        assert score == 4 and matched == ["address"]      # domain scored nothing
        assert len(consolidate_records(rows, {})[0]) == 2

    def test_cartridge_can_extend_the_denylist(self):
        config = {"consolidation": {"additional_noise_domains": ["rollupco.com"]}}
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://rollupco.com/alpha"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://rollupco.com/zeta"),
        ]
        assert len(consolidate_records(rows, config)[0]) == 2   # domain ignored
        assert len(consolidate_records(rows, {})[0]) == 1       # default: merges

    def test_cartridge_can_replace_the_denylist(self):
        config = {"consolidation": {"noise_domains": ["only-this.com"]}}
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://www.healthgrades.com/a"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://www.healthgrades.com/z"),
        ]
        # healthgrades is no longer denylisted, so the domain now counts.
        assert len(consolidate_records(rows, config)[0]) == 1

    def test_engine_default_carries_no_client_or_health_system_names(self):
        """RULE 3: the engine ships generic directory and host domains only."""
        assert "healthgrades.com" in NOISE_DOMAINS
        assert "facebook.com" in NOISE_DOMAINS
        for domain in NOISE_DOMAINS | UMBRELLA_DOMAINS:
            for banned in ("femasys", "neurolief", "proliv", "ormco", "obgyn"):
                assert banned not in domain


# ---------------------------------------------------------------------------
# Lossless merge
# ---------------------------------------------------------------------------

class TestLosslessMerge:

    def test_every_provider_and_source_row_survives(self):
        rows = [
            _row("T-1", "Jane Smith, MD", npi_number="1111111111",
                 provider_taxonomy_codes=["207V00000X"], specialty="OBGYN"),
            _row("T-2", "Ann Lee, DO", npi_number="2222222222",
                 provider_taxonomy_codes=["207VE0102X"], specialty="Fertility"),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1
        record = out[0]

        assert record["source_row_ids"] == ["T-1", "T-2"]
        assert record["provider_count"] == 2
        names = sorted(p["name"] for p in record["providers"])
        assert names == ["Ann Lee", "Jane Smith"]
        npis = sorted(p["npi"] for p in record["providers"])
        assert npis == ["1111111111", "2222222222"]
        credentials = {p["name"]: p["credentials"] for p in record["providers"]}
        assert credentials["Jane Smith"] == ["MD"] and credentials["Ann Lee"] == ["DO"]
        # Taxonomy is unioned across providers, never one physician's alone.
        assert record["provider_taxonomy_codes"] == ["207V00000X", "207VE0102X"]
        assert record["specialties"] == ["Fertility", "OBGYN"]

    def test_consolidation_block_explains_the_merge(self):
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999"),
        ]
        block = consolidate_records(rows, {})[0][0]["consolidation"]
        assert block["rule_fired"] == "merged"
        assert block["matched_fields"] == ["address", "domain"]
        assert block["score"] == 7
        assert block["merged_count"] == 2
        assert block["reviewed_by"] == ""       # filled by the override mechanism

    def test_unmerged_record_is_marked_single(self):
        block = consolidate_records([_row("T-1", "Solo Practice")], {})[0][0]["consolidation"]
        assert block["rule_fired"] == "single"
        assert block["merged_count"] == 1
        assert block["score"] == 0

    def test_merge_fills_fields_the_base_row_lacked(self):
        rows = [
            _row("T-1", "Alpha Womens Health", website="", phone="916-555-0100"),
            _row("T-2", "Alpha Womens Health", website="https://valleyclinic.com",
                 phone="916-555-0100"),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1
        assert out[0]["website_url"] == "https://valleyclinic.com"

    def test_organization_npi_name_wins_over_a_physician_name(self):
        rows = [
            _row("T-1", "Jane Smith MD", npi_entity_type="individual"),
            _row("T-2", "Sacramento Womens Medical Group",
                 npi_entity_type="organization",
                 npi_practice_name="Sacramento Womens Medical Group"),
        ]
        out, _ = consolidate_records(rows, {})
        assert out[0]["practice_name"] == "Sacramento Womens Medical Group"

    def test_unit_inside_the_street_line_is_persisted_as_its_own_field(self):
        """Sources ship "2800 L St #500" in one column. The parsed unit is the
        over-merge guard, so it must survive onto the record rather than living
        only inside the comparison keys."""
        rows = [_row("T-1", "Valley Clinic", street="2800 L St #500", unit="")]
        record = consolidate_records(rows, {})[0][0]
        assert record["address_unit"] == "suite 500"
        assert record["address_unit_normalized"] == "suite 500"
        # The normalized street never carries the unit.
        assert record["address_street_normalized"] == "2800 l street"

    def test_explicit_unit_column_is_left_alone(self):
        rows = [_row("T-1", "Valley Clinic", street="2800 L St", unit="Suite 500")]
        record = consolidate_records(rows, {})[0][0]
        assert record["address_unit"] == "Suite 500"          # raw value preserved
        assert record["address_unit_normalized"] == "suite 500"

    def test_organization_name_beats_a_physician_name(self):
        """A merged location is a place, not a person. With no organization NPI
        to borrow, an organization-shaped name still wins."""
        rows = [
            _row("T-1", "Andrew Fox, MD"),
            _row("T-2", "Sutter Medical Group"),
            _row("T-3", "Ardeep K Sekhon"),
        ]
        record = consolidate_records(rows, {})[0][0]
        assert record["practice_name"] == "Sutter Medical Group"

    def test_all_person_names_still_resolve_deterministically(self):
        rows = [_row("T-1", "Zeta Physician"), _row("T-2", "Alpha Person")]
        first = consolidate_records(rows, {})[0][0]["practice_name"]
        second = consolidate_records(list(reversed(rows)), {})[0][0]["practice_name"]
        assert first == second

    def test_input_records_are_not_mutated(self):
        rows = [_row("T-1", "Alpha"), _row("T-2", "Alpha")]
        consolidate_records(rows, {})
        assert all("providers" not in r for r in rows)
        assert [r["id"] for r in rows] == ["T-1", "T-2"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:

    def _fixture(self):
        return [
            _row("T-1", "Alpha Womens Health", street="123 Main St", unit="Suite 200"),
            _row("T-2", "Alpha Womens Health", street="123 Main St", unit=""),
            _row("T-3", "Beta Fertility", street="900 Oak Ave", zip_="95824",
                 website="https://beta-fertility.com"),
            _row("T-4", "Gamma Clinic", street="123 Main St", unit="Suite 400"),
        ]

    def test_same_input_same_practice_ids(self):
        first = consolidate_records(self._fixture(), {})[0]
        second = consolidate_records(self._fixture(), {})[0]
        assert [r["practice_id"] for r in first] == [r["practice_id"] for r in second]

    def test_input_order_does_not_change_the_result(self):
        forward = consolidate_records(self._fixture(), {})[0]
        reversed_rows = list(reversed(self._fixture()))
        backward = consolidate_records(reversed_rows, {})[0]
        assert [r["practice_id"] for r in forward] == [r["practice_id"] for r in backward]
        assert ([sorted(r["source_row_ids"]) for r in forward]
                == [sorted(r["source_row_ids"]) for r in backward])

    def test_practice_id_is_derived_from_normalized_keys(self):
        """Formatting differences in the input must not change the identity."""
        clean = consolidate_records([_row("T-1", "Valley Clinic",
                                          street="123 Main Street",
                                          zip_="95823")], {})[0]
        messy = consolidate_records([_row("T-9", "Valley Clinic, PLLC",
                                          street="123 Main St.",
                                          zip_="95823-4444")], {})[0]
        assert clean[0]["practice_id"] == messy[0]["practice_id"]

    def test_two_unmerged_practices_at_one_address_get_distinct_ids(self):
        """The review-queue case must not collide on a shared base key."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://alpha-health.com"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://zeta-fertility.com"),
        ]
        out, _ = consolidate_records(rows, {})
        assert len({r["practice_id"] for r in out}) == 2

    def test_practice_id_becomes_the_record_id(self):
        out, _ = consolidate_records([_row("T-1", "Solo")], {})
        assert out[0]["id"] == out[0]["practice_id"]
        assert out[0]["practice_id"].startswith("P-")


# ---------------------------------------------------------------------------
# Pass 2 — link, never merge
# ---------------------------------------------------------------------------

class TestPass2Grouping:

    def _six_offices(self):
        return [
            _row(f"T-{i}", f"Bay Medical Group Office {i}",
                 street=f"{i}00 Clinic Way", zip_=f"9582{i}",
                 phone=f"916-555-010{i}", website="https://baymedgroup.com")
            for i in range(1, 7)
        ]

    def test_six_offices_stay_six_records(self):
        out, summary = consolidate_records(self._six_offices(), {})
        assert len(out) == 6
        assert summary["merged_groups"] == 0
        assert summary["multi_location_groups"] == 1

    def test_group_fields_are_stamped(self):
        out, _ = consolidate_records(self._six_offices(), {})
        group_ids = {r["group_id"] for r in out}
        assert len(group_ids) == 1 and group_ids.pop().startswith("G-")
        assert all(r["location_count"] == 6 for r in out)
        assert sorted(r["location_index"] for r in out) == [1, 2, 3, 4, 5, 6]
        assert all(r["group_name"] for r in out)

    def test_location_index_is_deterministic(self):
        forward = consolidate_records(self._six_offices(), {})[0]
        backward = consolidate_records(list(reversed(self._six_offices())), {})[0]
        by_id_f = {r["practice_id"]: r["location_index"] for r in forward}
        by_id_b = {r["practice_id"]: r["location_index"] for r in backward}
        assert by_id_f == by_id_b

    def test_single_location_has_no_group(self):
        out, summary = consolidate_records([_row("T-1", "Solo Practice")], {})
        assert out[0]["group_id"] == ""
        assert out[0]["location_index"] == 1 and out[0]["location_count"] == 1
        assert summary["multi_location_groups"] == 0

    def test_denylisted_domain_does_not_group(self):
        rows = [
            _row("T-1", "Alpha", street="1 A St", zip_="95821",
                 website="https://www.healthgrades.com/a"),
            _row("T-2", "Beta", street="2 B St", zip_="95822",
                 website="https://www.healthgrades.com/b"),
        ]
        out, summary = consolidate_records(rows, {})
        assert summary["multi_location_groups"] == 0
        assert all(r["group_id"] == "" for r in out)

    def test_link_pass_never_merges(self):
        records = self._six_offices()
        before = len(records)
        link_location_groups(records, NOISE_DOMAINS)
        assert len(records) == before


# ---------------------------------------------------------------------------
# Configuration and edge cases
# ---------------------------------------------------------------------------

class TestConfiguration:

    def test_disabled_returns_input_untouched(self):
        rows = [_row("T-1", "Alpha"), _row("T-2", "Alpha")]
        out, summary = consolidate_records(rows, {"consolidation": {"enabled": False}})
        assert len(out) == 2
        assert summary["enabled"] is False
        assert all("providers" not in r for r in out)

    def test_enabled_by_default(self):
        assert consolidate_records([_row("T-1", "A")], {})[1]["enabled"] is True

    def test_empty_input(self):
        out, summary = consolidate_records([], {})
        assert out == [] and summary["output_count"] == 0

    def test_record_without_address_is_never_address_blocked(self):
        """No street or ZIP means no address key. With their own phones these
        rows have no contact key either, so they stay their own locations and
        the summary reports the gap rather than guessing."""
        rows = [
            _row("T-1", "Alpha", street="", zip_="", phone="916-555-0100"),
            _row("T-2", "Alpha", street="", zip_="", phone="916-555-0200"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["unblocked_count"] == 2
        assert summary["unblocked_rescued_by_contact"] == 0

    def test_summary_counts_add_up(self):
        rows = [
            _row("T-1", "Alpha Womens Health"),
            _row("T-2", "Alpha Womens Health"),
            _row("T-3", "Beta", street="900 Oak Ave", zip_="95824",
                 website="https://beta.com"),
        ]
        out, summary = consolidate_records(rows, {})
        assert summary["input_count"] == 3
        assert summary["output_count"] == len(out) == 2
        assert summary["rows_merged_away"] == 1
        assert summary["merged_groups"] == 1


# ---------------------------------------------------------------------------
# Two domain lists, two meanings
# ---------------------------------------------------------------------------

class TestNoiseVersusUmbrella:

    def test_umbrella_domain_is_merge_evidence_in_pass_1(self):
        """Shared ownership plus an identical street+ZIP (unit gate already run)
        is the same clinic. An umbrella domain must still score +3."""
        rows = [
            _row("T-1", "Alpha Womens Health", phone="916-555-0100",
                 website="https://www.sutterhealth.org/a"),
            _row("T-2", "Zeta Fertility Partners", phone="916-555-0999",
                 website="https://www.sutterhealth.org/z"),
        ]
        score, matched = score_pair(identity_of(rows[0]), identity_of(rows[1]),
                                    NOISE_DOMAINS)
        assert score == 7 and matched == ["address", "domain"]
        assert len(consolidate_records(rows, {})[0]) == 1

    def test_umbrella_domain_never_forms_a_pass_2_group(self):
        """Two hundred locations under one system domain are not a commercial
        group, so an umbrella domain must not link locations."""
        rows = [
            _row("T-1", "Alpha", street="1 A St", zip_="95821",
                 website="https://www.sutterhealth.org/a"),
            _row("T-2", "Beta", street="2 B St", zip_="95822",
                 website="https://www.sutterhealth.org/b"),
        ]
        out, summary = consolidate_records(rows, {})
        assert summary["multi_location_groups"] == 0
        assert all(r["group_id"] == "" for r in out)

    def test_noise_domain_is_excluded_from_both_passes(self):
        rows = [
            _row("T-1", "Alpha", street="1 A St", zip_="95821",
                 website="https://www.healthgrades.com/a"),
            _row("T-2", "Beta", street="2 B St", zip_="95822",
                 website="https://www.healthgrades.com/b"),
        ]
        out, summary = consolidate_records(rows, {})
        assert summary["multi_location_groups"] == 0          # not a group
        left, right = identity_of(rows[0]), identity_of(rows[1])
        assert "domain" not in score_pair(left, right, NOISE_DOMAINS)[1]

    def test_a_real_practice_domain_still_groups(self):
        """Two genuine offices of one group: own numbers, one website. They stay
        two locations (the contact block needs a shared phone too) and Pass 2
        links them into one group."""
        rows = [
            _row("T-1", "Bay Medical A", street="1 A St", zip_="95821",
                 phone="916-555-0100", website="https://baymedgroup.com"),
            _row("T-2", "Bay Medical B", street="2 B St", zip_="95822",
                 phone="916-555-0200", website="https://baymedgroup.com"),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 2
        assert summary["multi_location_groups"] == 1

    def test_each_list_is_independently_extendable(self):
        rows = [
            _row("T-1", "Alpha", street="1 A St", zip_="95821",
                 website="https://rollupco.com/a"),
            _row("T-2", "Beta", street="2 B St", zip_="95822",
                 website="https://rollupco.com/b"),
        ]
        # As an umbrella domain: no Pass 2 group, but still Pass 1 evidence.
        cfg = {"consolidation": {"additional_umbrella_domains": ["rollupco.com"]}}
        assert consolidate_records(rows, cfg)[1]["multi_location_groups"] == 0
        left, right = identity_of(rows[0]), identity_of(rows[1])
        noise, umbrella = domain_policy(cfg)
        assert "rollupco.com" in umbrella and "rollupco.com" not in noise
        assert score_pair(left, right, noise)[0] >= 0   # still comparable in Pass 1

    def test_lists_carry_no_client_or_specialty_names(self):
        for domain in NOISE_DOMAINS | UMBRELLA_DOMAINS:
            for banned in ("femasys", "neurolief", "proliv", "ormco", "obgyn"):
                assert banned not in domain


# ---------------------------------------------------------------------------
# Naming: never label a multi-provider location with a person's name
# ---------------------------------------------------------------------------

class TestNamingChain:

    def _cluster(self, names, website="https://valleyclinic.com", **over):
        rows = [_row(f"T-{i}", n, website=website, **over)
                for i, n in enumerate(names, start=1)]
        return consolidate_records(rows, {})[0][0]

    def test_npi_organization_name_wins(self):
        rows = [
            _row("T-1", "Andrew Fox"),
            _row("T-2", "Ardeep K Sekhon", npi_entity_type="organization",
                 npi_practice_name="Sutter Medical Foundation"),
        ]
        record = consolidate_records(rows, {})[0][0]
        assert record["practice_name"] == "Sutter Medical Foundation"
        assert record["practice_name_source"] == "npi_organization"

    def test_organization_shaped_source_name_is_next(self):
        record = self._cluster(["Andrew Fox", "Valley Medical Group", "A K Sekhon"])
        assert record["practice_name"] == "Valley Medical Group"
        assert record["practice_name_source"] == "source_row_organization"

    def test_domain_derived_when_every_name_is_a_person(self):
        """The 56-provider case: no organisation name anywhere, but the domain
        segments into recognisable words."""
        record = self._cluster(
            ["Andres Sciolla", "Andrew Fox", "Ardeep K Sekhon"],
            website="https://www.suttermedicalfoundation.org/x")
        assert record["practice_name"] == "Sutter Medical Foundation"
        assert record["practice_name_source"] == "domain_derived"

    def test_illegible_domain_falls_through_to_a_placeholder(self):
        """"smgdocs.com" yields nothing legible, and an honest placeholder beats
        a confidently wrong person's name."""
        record = self._cluster(
            ["Andres Sciolla", "Andrew Fox", "Ardeep K Sekhon"],
            website="https://smgdocs.com")
        assert record["practice_name"] == "3 providers at 123 Main St"
        assert record["practice_name_source"] == "placeholder"

    def test_a_person_never_labels_a_multi_provider_location(self):
        for site in ("https://smgdocs.com", "", "https://www.healthgrades.com/x"):
            record = self._cluster(
                ["Andres Sciolla", "Andrew Fox", "Ardeep K Sekhon"], website=site)
            assert "Sciolla" not in record["practice_name"]
            assert "Andrew Fox" not in record["practice_name"]

    def test_single_provider_location_keeps_its_person_name(self):
        """A solo practice really is named after the physician."""
        out, _ = consolidate_records([_row("T-1", "Andres Sciolla")], {})
        assert out[0]["practice_name"] == "Andres Sciolla"
        assert out[0]["practice_name_source"] == "source_row"

    def test_derivation_source_is_always_stamped(self):
        record = self._cluster(["Valley Medical Group", "Andrew Fox"])
        assert record["practice_name_source"] in {
            "npi_organization", "source_row_organization",
            "domain_derived", "placeholder", "source_row"}


# ---------------------------------------------------------------------------
# Contact blocking — a practice whose offices share one front desk
# ---------------------------------------------------------------------------

class TestContactBlocking:
    """The address block pins a location; the contact block catches a practice
    whose offices were scraped as separate rows behind one phone and one site.

    Two offices in different towns share no address block key, so before this
    they were never compared at all — nothing rejected the merge, the comparison
    never happened. Real case: one OBGYN group, two towns, one number, shipped as
    two billable accounts with byte-identical signals and two Claude calls.
    """

    _SITE = "https://www.groupobgyn.example"
    _PHONE = "404-555-0142"

    def _two_towns(self, **over):
        site = over.pop("website", self._SITE)
        return [
            _row("T-1", "Group OBGYN", street="100 North Office Pkwy",
                 zip_="30041", phone=self._PHONE, website=site, **over),
            _row("T-2", "Group OBGYN", street="200 South Office Blvd",
                 zip_="30024", phone=self._PHONE, website=site, **over),
        ]

    def test_shared_phone_and_domain_merges_across_towns(self):
        out, summary = consolidate_records(self._two_towns(), {})
        assert len(out) == 1
        assert summary["cross_address_merges"] == 1

    def test_umbrella_domain_never_enters_the_contact_block(self):
        """Two clinics of one health system on a central appointment line.

        Pass 1 counts an umbrella domain as merge evidence elsewhere because
        street and ZIP had already pinned the location. Here nothing has, so the
        exemption must not carry over.
        """
        umbrella = "https://" + sorted(UMBRELLA_DOMAINS)[0]
        out, summary = consolidate_records(self._two_towns(website=umbrella), {})
        assert len(out) == 2
        assert summary["cross_address_merges"] == 0

    def test_noise_domain_never_enters_the_contact_block(self):
        noise = "https://" + sorted(NOISE_DOMAINS)[0]
        out, _ = consolidate_records(self._two_towns(website=noise), {})
        assert len(out) == 2

    def test_same_domain_different_phones_stays_split(self):
        """The whole selectivity argument: a multi-office group whose offices
        have their own numbers is left alone. Domain alone must not merge."""
        rows = self._two_towns()
        rows[1]["phone"] = "770-555-0177"
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_missing_phone_or_domain_stays_split(self):
        for field in ("phone", "website_url"):
            rows = self._two_towns()
            rows[1][field] = ""
            out, _ = consolidate_records(rows, {})
            assert len(out) == 2, f"merged with no {field} on one side"

    def test_different_suites_in_different_towns_still_merge(self):
        """The defect this class was written to fix, and first got wrong.

        A suite number answers "which door in this building". Two offices in two
        towns of course carry different suite numbers, so vetoing on that fired
        on exactly the pairs the contact block exists to recognise — and it
        fired BEFORE scoring, so nothing downstream could recover it. Every
        multi-office practice on earth has this shape.
        """
        rows = self._two_towns()
        rows[0]["address_unit"] = "Suite 1200"
        rows[1]["address_unit"] = "Suite 640"
        out, summary = consolidate_records(rows, {})
        assert len(out) == 1
        assert summary["cross_address_merges"] == 1

    def test_four_offices_four_towns_become_one_account(self):
        """The commercial point: one practice, one buying decision, one dial."""
        rows = [
            _row(f"T-{n}", "Group OBGYN", street=f"{n}00 Office Way",
                 unit=f"Suite {n}00", zip_=f"3004{n}", phone=self._PHONE,
                 website=self._SITE)
            for n in range(1, 5)
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1
        assert sorted(out[0]["source_row_ids"]) == ["T-1", "T-2", "T-3", "T-4"]

    def test_same_building_suite_veto_is_untouched(self):
        """The ruling that settled the suite question stands: within ONE
        building, differing suites are different practices and never merge."""
        rows = self._two_towns()
        rows[1]["address_street"] = rows[0]["address_street"]
        rows[1]["address_zip"] = rows[0]["address_zip"]
        rows[0]["address_unit"] = "Suite 200"
        rows[1]["address_unit"] = "Suite 400"
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_same_building_transitivity_guard_is_untouched(self):
        """Suite 200 and Suite 400 must not join through a unit-less sibling."""
        rows = [
            _row("T-1", "Alpha Clinic", unit="Suite 200"),
            _row("T-2", "Alpha Clinic", unit=""),
            _row("T-3", "Alpha Clinic", unit="Suite 400"),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_contact_path_adds_no_review_work(self):
        """A pair reaching this block already carries phone + domain, which is
        exactly MERGE_THRESHOLD, so it can never land in the review band."""
        out, summary = consolidate_records(self._two_towns(), {})
        assert len(out) == 1
        assert summary["review_pairs"] == 0
        assert summary["review_reasons"] == {}

    def test_missing_zip_does_not_disarm_the_suite_veto(self):
        """Same street, one ZIP never captured, different suites. Almost
        certainly one building — the ruling says these stay apart. Strict
        street+ZIP equality would silently disarm the veto on exactly the
        malformed rows most likely to be duplicates."""
        rows = self._two_towns()
        for r in rows:
            r["address_street"] = "100 North Office Pkwy"
        rows[0]["address_unit"], rows[0]["address_zip"] = "Suite 200", "30041"
        rows[1]["address_unit"], rows[1]["address_zip"] = "Suite 400", ""
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_missing_zip_veto_holds_transitively(self):
        """The ZIP-less Suite 400 row must not reach Suite 200 through a
        unit-less bridge either — the cluster guard uses the same rule as the
        pairwise gate."""
        base = dict(self._two_towns()[0], address_street="100 North Office Pkwy")
        rows = [
            dict(base, id="T-1", address_unit="Suite 200", address_zip="30041"),
            dict(base, id="T-2", address_unit="", address_zip="30041"),
            dict(base, id="T-3", address_unit="Suite 400", address_zip=""),
        ]
        out, _ = consolidate_records(rows, {})
        assert len(out) == 2

    def test_same_street_name_in_two_towns_is_not_a_conflict(self):
        """Both ZIPs present and different is positive evidence of two
        buildings — one street name in two towns. Units coexist and the shared
        front desk merges them."""
        rows = self._two_towns()
        for r in rows:
            r["address_street"] = "100 Main St"
        rows[0]["address_unit"] = "Suite 300"
        rows[1]["address_unit"] = "Suite 1201"
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1


    def test_oversized_block_is_skipped_and_counted(self):
        """A number on many rows is an answering service, not a front desk.
        Skipping it silently would read as 'nothing to merge here'."""
        rows = [
            _row(f"T-{i}", f"Practice {i}", street=f"{i} Main St",
                 zip_=f"300{i:02d}", phone="404-555-0199",
                 website="https://sharedbilling.example")
            for i in range(MAX_CONTACT_BLOCK + 3)
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == len(rows)
        assert summary["contact_blocks_skipped_oversized"] == 1

    def test_block_at_the_cap_is_still_scored(self):
        rows = [
            _row(f"T-{i}", "Group OBGYN", street=f"{i} Main St",
                 zip_=f"300{i:02d}", phone=self._PHONE, website=self._SITE)
            for i in range(MAX_CONTACT_BLOCK)
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 1
        assert summary["contact_blocks_skipped_oversized"] == 0

    def test_config_can_turn_it_off(self):
        out, summary = consolidate_records(
            self._two_towns(), {"consolidation": {"contact_blocking": False}})
        assert len(out) == 2
        assert summary["cross_address_merges"] == 0

    def test_config_can_raise_the_cap(self):
        rows = [
            _row(f"T-{i}", "Group OBGYN", street=f"{i} Main St",
                 zip_=f"300{i:02d}", phone=self._PHONE, website=self._SITE)
            for i in range(MAX_CONTACT_BLOCK + 2)
        ]
        out, _ = consolidate_records(
            rows, {"consolidation": {"max_contact_block": MAX_CONTACT_BLOCK + 5}})
        assert len(out) == 1

    def test_address_unblocked_row_reached_by_contact_is_reported(self):
        """A row with no street cannot be address-blocked (backlog 23). When the
        contact block reaches it, unblocked_count must still report the address
        gap rather than being quietly redefined by this feature."""
        rows = self._two_towns()
        rows[1]["address_street"] = ""
        out, summary = consolidate_records(rows, {})
        assert len(out) == 1
        assert summary["unblocked_count"] == 1
        assert summary["unblocked_rescued_by_contact"] == 1

    def test_same_address_pair_is_judged_once(self):
        """A pair sitting in BOTH blocks must not be scored twice — a duplicate
        review edge would inflate the queue for one relationship."""
        rows = [
            _row("T-1", "Group OBGYN", phone=self._PHONE, website=self._SITE),
            _row("T-2", "Group OBGYN", phone=self._PHONE, website=self._SITE),
        ]
        out, summary = consolidate_records(rows, {})
        assert len(out) == 1
        # One address-block merge, so the contact path contributed nothing new.
        assert summary["cross_address_merges"] == 0

    def test_merge_is_order_independent(self):
        rows = self._two_towns()
        forward, _ = consolidate_records([dict(r) for r in rows], {})
        reverse, _ = consolidate_records([dict(r) for r in reversed(rows)], {})
        assert len(forward) == len(reverse) == 1
        assert forward[0]["practice_id"] == reverse[0]["practice_id"]

    def test_merged_record_keeps_both_source_rows(self):
        out, _ = consolidate_records(self._two_towns(), {})
        assert sorted(out[0]["source_row_ids"]) == ["T-1", "T-2"]


class TestCrossBuildingAddressIntegrity:
    """A merged record's address is one fact, not five independent fields.

    Per-field gap-fill was safe while every cluster member shared a building
    (the address block guaranteed it). The contact path merges across
    buildings, where independent fill stitched building A's street to building
    B's suite — or a wrong-town ZIP onto the base street — and shipped an
    address that does not exist to the rep's Directions link and the
    practice_id hash.
    """

    _SITE = "https://www.groupobgyn.example"
    _PHONE = "404-555-0142"

    def _office(self, rid, street, unit, city, zipc, **over):
        rec = _row(rid, "Group OBGYN", street=street, unit=unit, zip_=zipc,
                   phone=self._PHONE, website=self._SITE)
        rec["address_city"] = city
        rec.update(over)
        return rec

    def _cross_building(self, base_over=None, sibling_over=None):
        """Base in building A (org NPI so it wins base), sibling in building B."""
        base = self._office("T-A", "100 North Office Pkwy", "", "Northtown",
                            "30041", npi_entity_type="organization")
        base.update(base_over or {})
        sibling = self._office("T-B", "200 South Office Blvd", "Suite 1201",
                               "Southport", "30024")
        sibling.update(sibling_over or {})
        return [base, sibling]

    def test_sibling_suite_never_lands_on_the_base_street(self):
        out, _ = consolidate_records(self._cross_building(), {})
        assert len(out) == 1
        rec = out[0]
        assert rec["address_street"] == "100 North Office Pkwy"
        assert rec["address_unit"] == ""      # Suite 1201 belongs to building B
        assert rec["address_zip"] == "30041"

    def test_wrong_town_zip_never_completes_the_base_street(self):
        """An honest incomplete address beats a confident wrong one."""
        out, _ = consolidate_records(
            self._cross_building(base_over={"address_zip": ""}), {})
        rec = out[0]
        assert rec["address_street"] == "100 North Office Pkwy"
        assert rec["address_zip"] == ""       # not the sibling town's 30024

    def test_same_building_suite_fill_still_works(self):
        """The fill the suite ruling relies on: a unit-less base row learns its
        suite from a same-building sibling."""
        rows = self._cross_building(
            sibling_over={"address_street": "100 North Office Pkwy",
                          "address_city": "Northtown", "address_zip": "30041",
                          "address_unit": "Suite 300"})
        out, _ = consolidate_records(rows, {})
        assert len(out) == 1
        assert out[0]["address_unit"] == "Suite 300"

    def test_streetless_base_adopts_one_donor_address_wholesale(self):
        """A base row naming no building takes a sibling's WHOLE address —
        including overwriting its stray town — never a hybrid of the two."""
        rows = self._cross_building(
            base_over={"address_street": "", "address_city": "Southport",
                       "address_zip": ""})
        out, _ = consolidate_records(rows, {})
        rec = out[0]
        assert rec["address_street"] == "200 South Office Blvd"
        assert rec["address_unit"] == "Suite 1201"
        assert rec["address_city"] == "Southport"
        assert rec["address_zip"] == "30024"

    def test_non_address_fields_still_fill_from_any_sibling(self):
        rows = self._cross_building(base_over={"specialty": ""})
        out, _ = consolidate_records(rows, {})
        assert out[0]["specialty"] == "OBGYN"
