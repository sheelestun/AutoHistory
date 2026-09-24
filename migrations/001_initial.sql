CREATE TABLE users (
 id UUID PRIMARY KEY, telegram_user_id BIGINT NOT NULL UNIQUE,
 timezone TEXT NOT NULL, delivery_enabled BOOLEAN NOT NULL DEFAULT true,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE cars (
 id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users ON DELETE CASCADE,
 creation_key UUID NOT NULL UNIQUE, creation_payload JSONB NOT NULL,
 brand VARCHAR(60) NOT NULL CHECK(length(trim(brand))>0),
 model VARCHAR(60) NOT NULL CHECK(length(trim(model))>0),
 year SMALLINT NOT NULL CHECK(year BETWEEN 1900 AND 9999),
 odometer_km INTEGER NOT NULL CHECK(odometer_km BETWEEN 0 AND 2000000),
 odometer_as_of DATE NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX cars_owner ON cars(user_id);
CREATE TABLE maintenance_records (
 id UUID PRIMARY KEY, car_id UUID NOT NULL REFERENCES cars ON DELETE CASCADE,
 creation_key UUID NOT NULL UNIQUE, creation_payload JSONB NOT NULL,
 service_date DATE NOT NULL, odometer_km INTEGER NOT NULL CHECK(odometer_km BETWEEN 0 AND 2000000),
 work_code TEXT NOT NULL CHECK(work_code IN ('ENGINE_OIL','OIL_FILTER','AIR_FILTER','CABIN_FILTER','BRAKE_FLUID','OTHER')),
 custom_work VARCHAR(60), cost_rub NUMERIC(12,2) CHECK(cost_rub BETWEEN 0 AND 10000000),
 station VARCHAR(120), note VARCHAR(500), photo_file_id TEXT, photo_unique_id TEXT, photo_kind TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK((work_code='OTHER' AND custom_work IS NOT NULL AND length(trim(custom_work))>0) OR (work_code<>'OTHER' AND custom_work IS NULL)),
 CHECK((photo_file_id IS NULL AND photo_unique_id IS NULL AND photo_kind IS NULL) OR
 (photo_file_id IS NOT NULL AND photo_unique_id IS NOT NULL AND photo_kind IS NOT NULL AND photo_kind IN ('photo','document')))
);
CREATE INDEX records_history ON maintenance_records(car_id,service_date DESC,odometer_km DESC,id);
CREATE TABLE maintenance_plans (
 id UUID PRIMARY KEY, car_id UUID NOT NULL REFERENCES cars ON DELETE CASCADE,
 work_code TEXT NOT NULL CHECK(work_code IN ('ENGINE_OIL','OIL_FILTER','AIR_FILTER','CABIN_FILTER','BRAKE_FLUID')),
 interval_km INTEGER CHECK(interval_km BETWEEN 100 AND 100000),
 interval_months INTEGER CHECK(interval_months BETWEEN 1 AND 60),
 initial_date DATE NOT NULL, initial_odometer_km INTEGER NOT NULL CHECK(initial_odometer_km BETWEEN 0 AND 2000000),
 last_record_id UUID REFERENCES maintenance_records ON DELETE SET NULL,
 cycle_no INTEGER NOT NULL DEFAULT 1 CHECK(cycle_no>0), active BOOLEAN NOT NULL DEFAULT true,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(car_id,work_code), CHECK(interval_km IS NOT NULL OR interval_months IS NOT NULL)
);
CREATE TABLE notification_outbox (
 id UUID PRIMARY KEY, car_id UUID NOT NULL REFERENCES cars ON DELETE CASCADE,
 plan_id UUID REFERENCES maintenance_plans ON DELETE CASCADE,
 event_type TEXT NOT NULL CHECK(event_type IN ('maintenance','odometer')),
 stage TEXT NOT NULL CHECK(stage IN ('soon','due','refresh')), cycle_no INTEGER,
 dedupe_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','cancelled','failed')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 sent_at TIMESTAMPTZ, telegram_message_id BIGINT, last_error TEXT,
 CHECK((event_type='maintenance' AND plan_id IS NOT NULL AND cycle_no IS NOT NULL AND stage IN ('soon','due')) OR
 (event_type='odometer' AND plan_id IS NULL AND cycle_no IS NULL AND stage='refresh'))
);
CREATE INDEX outbox_ready ON notification_outbox(status,next_attempt_at);
CREATE TABLE job_health (name TEXT PRIMARY KEY, last_success_at TIMESTAMPTZ NOT NULL);
