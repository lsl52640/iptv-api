CREATE TABLE IF NOT EXISTS public.v_channel (
	id character(128) NOT NULL,
	title character varying(255) NOT NULL,
	code character varying(255) NOT NULL,
	pic character varying(255),
	status integer NOT NULL,
	create_time timestamp without time zone DEFAULT now(),
	update_time timestamp without time zone DEFAULT now(),
	channel_group character varying(255),
	url jsonb,
	url_ip6 jsonb,
	PRIMARY KEY(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_v_channel_code ON public.v_channel(code);