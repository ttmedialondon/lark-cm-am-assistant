-- The former suggestion-only feature had no confidence control in its UI.
-- Introduce a 95% production default; retain credentials and enabled state.
UPDATE ai_feature_configs SET auto_confirm_threshold=0.950
WHERE feature='creator_responsibility_matching' AND auto_confirm_threshold=0.900;
