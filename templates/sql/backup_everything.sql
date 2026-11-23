-- Every user database backed up to /var/opt/mssql/backup/<name>.bak. harbor sql backup-everything prepares the
-- folder's permissions first. WITH INIT replaces the backup sets in each existing file.
-- A row per database, as it is done (ROW|nr|name|size in KB|seconds): harbor makes a table of them, so what was
-- backed up before an error is still shown.
SET NOCOUNT ON

DECLARE @name SYSNAME
DECLARE @fileName NVARCHAR(512)
DECLARE @nr INT = 0
DECLARE @started DATETIME2
DECLARE @kilobytes BIGINT

DECLARE db_cursor CURSOR READ_ONLY FOR
SELECT name
FROM master.sys.databases
WHERE name NOT IN ('master','model','msdb','tempdb')  -- exclude these databases
AND state = 0 -- database is online
AND is_in_standby = 0 -- database is not read only for log shipping
ORDER BY name

OPEN db_cursor
FETCH NEXT FROM db_cursor INTO @name

WHILE @@FETCH_STATUS = 0
BEGIN
	SET @fileName = N'/var/opt/mssql/backup/' + @name + N'.bak'
	SET @started = SYSDATETIME()
	BACKUP DATABASE @name TO DISK = @fileName WITH INIT
	SET @nr = @nr + 1
	SET @kilobytes = (SELECT TOP 1 CAST(backup_size / 1024 AS BIGINT) FROM msdb.dbo.backupset
		WHERE database_name = @name AND type = 'D' ORDER BY backup_set_id DESC)
	SELECT 'ROW|' + CAST(@nr AS VARCHAR(10)) + '|' + @name + '|' + CAST(ISNULL(@kilobytes, 0) AS VARCHAR(20)) + '|'
		+ CAST(CAST(DATEDIFF(MILLISECOND, @started, SYSDATETIME()) / 1000.0 AS DECIMAL(10, 1)) AS VARCHAR(20))
	FETCH NEXT FROM db_cursor INTO @name
END

CLOSE db_cursor
DEALLOCATE db_cursor
