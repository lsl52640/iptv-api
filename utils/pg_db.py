import os
import json
import datetime
from utils.config import config
from utils.i18n import t

def save_to_postgresql(json_data):
    """
    Save channel json data to PostgreSQL database in table v_channel directly using psycopg (v3)
    """
    if not config.open_pg:
        return

    try:
        import psycopg
        from psycopg.types.json import Json
    except ImportError:
        print("❌ [PostgreSQL] psycopg (v3) is not installed. Please install it first.")
        return

    conn = None
    try:
        # psycopg.connect supports kwargs similar to psycopg2
        conn = psycopg.connect(
            host=config.pg_host,
            port=config.pg_port,
            user=config.pg_user,
            password=config.pg_password,
            dbname=config.pg_database, # In psycopg3, dbname is used instead of database
            conninfo="" # We can rely on kwargs
        )
        cursor = conn.cursor()

        # Truncate table to refresh all records
        cursor.execute("TRUNCATE TABLE public.v_channel;")

        # Insert query
        insert_query = """
        INSERT INTO public.v_channel (id, title, code, pic, status, create_time, update_time, channel_group, url, url_ip6)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """

        now = datetime.datetime.now()

        # Prepare batch data
        batch_data = []
        import hashlib
        
        for item in json_data:
            name = item.get("name", "")
            tvg_id = item.get("tvg_id") or name
            tvg_logo = item.get("tvg_logo", "")
            group = item.get("group", "")
            urls = item.get("url", [])
            
            # Separate IPv4 and IPv6 URLs
            ipv4_urls = []
            ipv6_urls = []
            for u in urls:
                if "://[" in u:
                    ipv6_urls.append(u)
                else:
                    ipv4_urls.append(u)
            
            # Generate unique ID based on name and group hash
            unique_str = f"{name}_{group}"
            item_id = hashlib.md5(unique_str.encode("utf-8")).hexdigest()
            
            batch_data.append((
                item_id,
                name,
                tvg_id,
                tvg_logo,
                1, # status = 1 (active)
                now, # create_time
                now, # update_time
                group,
                Json(ipv4_urls), # url (jsonb)
                Json(ipv6_urls)  # url_ip6 (jsonb)
            ))

        if batch_data:
            cursor.executemany(insert_query, batch_data)

        conn.commit()
        print(f"✅ [PostgreSQL] Successfully saved {len(batch_data)} channels to table 'public.v_channel' via psycopg (v3).")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ [PostgreSQL] Error saving to public.v_channel: {e}")
    finally:
        if conn:
            conn.close()
