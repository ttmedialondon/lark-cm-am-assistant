ALTER TABLE assistant_rule_overrides
    DROP CONSTRAINT assistant_rule_overrides_rule_key_check,
    ADD COLUMN title TEXT NOT NULL DEFAULT '助手文档',
    ADD COLUMN description TEXT NOT NULL DEFAULT '',
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'rule' CHECK (kind IN ('rule','manual','knowledge','template')),
    ADD COLUMN status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('draft','published','archived')),
    ADD COLUMN visibility TEXT NOT NULL DEFAULT 'all' CHECK (visibility IN ('all','owner'));

UPDATE assistant_rule_overrides SET title = CASE rule_key
    WHEN 'core' THEN '核心规则' WHEN 'understand' THEN '理解规则'
    WHEN 'evidence' THEN '证据规则' WHEN 'answer' THEN '回答规则' END,
    description = CASE rule_key
    WHEN 'core' THEN '身份、权限和基本边界' WHEN 'understand' THEN '时间、范围和连续追问'
    WHEN 'evidence' THEN '查询、来源和分段归纳' WHEN 'answer' THEN '业务归纳、卡片和回复表达' END;

ALTER TABLE assistant_rule_overrides
    ADD CONSTRAINT assistant_document_title_length CHECK (char_length(title) BETWEEN 1 AND 160),
    ADD CONSTRAINT assistant_document_description_length CHECK (char_length(description) <= 1000),
    ADD CONSTRAINT assistant_document_core_metadata CHECK (
        (rule_key IN ('core','understand','evidence','answer')) = (kind = 'rule')
        AND (kind <> 'rule' OR (status = 'published' AND visibility = 'all'))
    );
