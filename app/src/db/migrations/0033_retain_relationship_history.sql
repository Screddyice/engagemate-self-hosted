ALTER TABLE relationship_follows
    DROP CONSTRAINT relationship_follows_target_scope_fk,
    DROP CONSTRAINT relationship_follows_engagement_scope_fk,
    ADD CONSTRAINT relationship_follows_target_scope_fk
        FOREIGN KEY (target_id,ig_account_id,user_id)
        REFERENCES targets(id,ig_account_id,user_id) ON DELETE NO ACTION,
    ADD CONSTRAINT relationship_follows_engagement_scope_fk
        FOREIGN KEY (prerequisite_engagement_id,ig_account_id,user_id)
        REFERENCES story_engagements(id,ig_account_id,user_id) ON DELETE NO ACTION;
