-- Vault helper RPCs for storing/reading secrets from Python agent and edge functions.

-- Store a secret (upsert)
CREATE OR REPLACE FUNCTION set_vault_secret(p_name text, p_secret text)
RETURNS void AS $$
BEGIN
  INSERT INTO vault.secrets (name, secret)
  VALUES (p_name, p_secret)
  ON CONFLICT (name) DO UPDATE SET secret = EXCLUDED.secret;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Read a secret
CREATE OR REPLACE FUNCTION get_vault_secret(p_name text)
RETURNS text AS $$
  SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = p_name LIMIT 1;
$$ LANGUAGE sql SECURITY DEFINER;

-- Delete a secret
CREATE OR REPLACE FUNCTION delete_vault_secret(p_name text)
RETURNS void AS $$
  DELETE FROM vault.secrets WHERE name = p_name;
$$ LANGUAGE sql SECURITY DEFINER;
