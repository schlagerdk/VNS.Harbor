-- Every user database's log made as small as it goes. A log that is already at its smallest (two virtual log
-- files: SQL Server keeps at least two) is left alone, so its recovery model is not switched for nothing and no
-- "Cannot shrink log file" message is written for it. The log is found by its type, not by a name ending in _log.
SET NOCOUNT ON

DECLARE @name SYSNAME
DECLARE @SQL NVARCHAR(MAX)

DECLARE db_cursor CURSOR READ_ONLY FOR
SELECT name
FROM master.sys.databases
WHERE name NOT IN ('master','model','msdb','tempdb')  -- exclude these databases
AND state = 0 -- database is online
AND is_in_standby = 0 -- database is not read only for log shipping

OPEN db_cursor
FETCH NEXT FROM db_cursor INTO @name

WHILE @@FETCH_STATUS = 0
BEGIN
	IF (SELECT COUNT(*) FROM sys.dm_db_log_info(DB_ID(@name))) > 2
	BEGIN
		-- Used Dynamic SQL for all databases.
		SET @SQL = N'USE ' + QUOTENAME(@name) + N';' + CHAR(10)
			+ N'ALTER DATABASE ' + QUOTENAME(@name) + N' SET RECOVERY SIMPLE;' + CHAR(10)
			+ N'ALTER DATABASE ' + QUOTENAME(@name) + N' SET RECOVERY FULL;' + CHAR(10)
			+ N'DECLARE @log INT = (SELECT TOP 1 file_id FROM sys.database_files WHERE type = 1);' + CHAR(10)
			+ N'DBCC SHRINKFILE (@log, 1) WITH NO_INFOMSGS;'
		EXEC (@SQL)
		PRINT 'Shrank the log of ' + @name
	END
	FETCH NEXT FROM db_cursor INTO @name
END

CLOSE db_cursor
DEALLOCATE db_cursor
