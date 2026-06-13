# -*- coding: utf-8 -*-
"""
Created on Sat Jun 13 11:26:09 2026

@author: AHema
"""

import duckdb
con = duckdb.connect(r"data\db\kenya_fnp.duckdb")
print(con.execute("""
  select table_schema, table_name
  from information_schema.tables
  where table_schema in ('core','geography','soil','food','health','policy')
  order by 1,2
""").df().to_string(index=False))
con.close()