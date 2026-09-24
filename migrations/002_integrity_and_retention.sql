ALTER TABLE notification_outbox ADD COLUMN retired_at TIMESTAMPTZ;
CREATE INDEX outbox_retention ON notification_outbox(retired_at) WHERE retired_at IS NOT NULL;

CREATE FUNCTION validate_plan_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.last_record_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM maintenance_records r WHERE r.id=NEW.last_record_id
   AND r.car_id=NEW.car_id AND r.work_code=NEW.work_code
 ) THEN
   RAISE EXCEPTION 'plan base must belong to the same car and work type' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER plan_record_integrity BEFORE INSERT OR UPDATE ON maintenance_plans
FOR EACH ROW EXECUTE FUNCTION validate_plan_record();

CREATE FUNCTION validate_user_timezone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_timezone_names WHERE name=NEW.timezone) THEN
   RAISE EXCEPTION 'invalid timezone' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER user_timezone_integrity BEFORE INSERT OR UPDATE OF timezone ON users
FOR EACH ROW EXECUTE FUNCTION validate_user_timezone();
