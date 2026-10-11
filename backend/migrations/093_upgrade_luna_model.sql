-- Change the configured model without re-enabling features or restarting their queues.
UPDATE ai_feature_configs
SET model = 'gpt-6-luna',
    last_tested_at = NULL,
    last_test_status = 'never',
    updated_at = NOW()
WHERE provider = 'openai' AND model = 'gpt-5.6-luna';
