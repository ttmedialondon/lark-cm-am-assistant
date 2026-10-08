-- Expand ranking only; preserve facts, queues, manual decisions and API configuration.
ALTER TABLE creator_importance_profiles
  DROP CONSTRAINT creator_importance_profiles_auto_tier_check,
  ADD CONSTRAINT creator_importance_profiles_auto_tier_check
    CHECK(auto_tier IN ('focus','priority','normal','dormant','pending')),
  DROP CONSTRAINT creator_importance_profiles_manual_tier_check,
  ADD CONSTRAINT creator_importance_profiles_manual_tier_check
    CHECK(manual_tier IN ('focus','priority','normal','dormant','pending'));
