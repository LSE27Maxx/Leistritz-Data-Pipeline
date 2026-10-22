# Leistritz_data_processing.ipynb — replacement cells (Step 168, 2026-10-22)
# Paste CELL 1 over the code cell under "Creation of a dictionary with all of the files from the bucket",
# and CELL 2 over the code cell under "Cleaning and reshaping of the files". Then run all cells.

# ===================== CELL 1: load =====================
# Set to True to rebuild every CSV in Drive (e.g. after changing the cleaning step below)
REPROCESS_ALL = False

already_done = set() if REPROCESS_ALL else {
    os.path.splitext(name)[0] for name in os.listdir(output_folder) if name.endswith('.csv')
}

# Copies of other exports in the bucket -- skipped so the same run doesn't appear twice on the dashboard
EXCLUDE = {
    '240403R Ben Jones & Broadway-3',     # identical to ...Broadway-2
    'AM AA AA AB 241015 AP PR 0083(1)',   # identical to ...PR 0083
    'Untitled spreadsheet',               # trimmed copy of 240403R Ben Jones & Broadway-2, timestamps misaligned
    '240712F - 990',                      # trimmed copy of 240712F - 990(1), timestamps misaligned
}

dfs = {}
dfs_range = {}
columns = {}
skipped = 0

for blob in bucket.list_blobs():
  if blob.name.endswith('.csv'):
    file_name = blob.name.split('/')[-1]
    file_name = file_name.split('.')[0]
    parts = file_name.split('_')
    file_name = parts[0]
    if len(parts) > 2:
      dfs_range[file_name] = [parts[1], parts[2]]
    if file_name in already_done or file_name in EXCLUDE:
      skipped += 1
      continue
    already_done.add(file_name)
    with fs.open(f'gs://{bucket_name}/{blob.name}', 'rb') as f:
      # Machine exports use ';' but some files were re-saved (e.g. via Excel) with ','
      first_line = f.readline().decode('utf-8', errors='replace')
      f.seek(0)
      delimiter = ';' if first_line.count(';') >= first_line.count(',') else ','
      df = pd.read_csv(f, delimiter=delimiter)
      dfs[file_name] = df
      columns[file_name] = df.columns.tolist()

print(f"{len(dfs)} new file(s) to process, {skipped} already in Drive or excluded, and skipped.")

# ===================== CELL 2: clean and write =====================
# Machine column -> English name, in output order. Date/Time come from TIME.
column_names = {
    "DOS_01_IW_Behael_Gewicht": "Feeder 1 Hopper Weight (kg)",
    "DOS_01_IW_Drehzahl_Proz": "Feeder 1 Speed (%)",
    "DOS_01_IW_kg_h": "Feeder 1 Actual Throughput (kg/h)",
    "DOS_01_SW_kg_h": "Feeder 1 Set Throughput (kg/h)",
    "DOS_02_IW_Behael_Gewicht": "Feeder 2 Hopper Weight (kg)",
    "DOS_02_IW_Drehzahl_Proz": "Feeder 2 Speed (%)",
    "DOS_02_IW_kg_h": "Feeder 2 Actual Throughput (kg/h)",
    "DOS_02_SW_kg_h": "Feeder 2 Set Throughput (kg/h)",
    "DOS_03_IW_Behael_Gewicht": "Feeder 3 Hopper Weight (kg)",
    "DOS_03_IW_Drehzahl_Proz": "Feeder 3 Speed (%)",
    "DOS_03_IW_kg_h": "Feeder 3 Actual Throughput (kg/h)",
    "DOS_03_SW_kg_h": "Feeder 3 Set Throughput (kg/h)",
    "DOS_04_IW_Behael_Gewicht": "Feeder 4 Hopper Weight (kg)",
    "DOS_04_IW_Drehzahl_Proz": "Feeder 4 Speed (%)",
    "DOS_04_IW_kg_h": "Feeder 4 Actual Throughput (kg/h)",
    "DOS_04_SW_kg_h": "Feeder 4 Set Throughput (kg/h)",
    "DOS_Gesamtistwert_kg_h": "Total Feeder Throughput (kg/h)",
    "HZG_00_Temp_Istwert": "Actual Barrel Temperature Zone Feeder",
    "HZG_00_Temp_Sollwert": "Setpoint Barrel Temperature Zone Feeder",
    "HZG_01_Temp_Istwert": "Actual Barrel Temperature Zone 1",
    "HZG_02_Temp_Istwert": "Actual Barrel Temperature Zone 2",
    "HZG_03_Temp_Istwert": "Actual Barrel Temperature Zone 3",
    "HZG_04_Temp_Istwert": "Actual Barrel Temperature Zone 4",
    "HZG_05_Temp_Istwert": "Actual Barrel Temperature Zone 5",
    "HZG_06_Temp_Istwert": "Actual Barrel Temperature Zone 6",
    "HZG_07_Temp_Istwert": "Actual Barrel Temperature Zone 7",
    "HZG_08_Temp_Istwert": "Actual Barrel Temperature Zone 8",
    "HZG_09_Temp_Istwert": "Actual Barrel Temperature Zone 9",
    "HZG_10_Temp_Istwert": "Actual Barrel Temperature Zone 10",
    "HZG_11_Temp_Istwert": "Actual Barrel Temperature Zone 11",
    "HZG_12_Temp_Istwert": "Actual Barrel Temperature Zone Vent",
    "iExt_Anzeige": "Torque (%)",
    "MDru1_AnzeigeIst": "Melt Pressure (bar)",
    "MTemp1_Ist_Real": "Melt Temperature (oC)",
    "nExtAnzeige": "Extruder speed (rpm)",
    "Vak_1_Anzeige": "Vacuum Pressure (bar)",
    "HZG_00_EDInt": "HZG_00_EDInt",
    "HZG_01_EDInt": "HZG_01_EDInt",
    "HZG_02_EDInt": "HZG_02_EDInt",
    "HZG_03_EDInt": "HZG_03_EDInt",
    "HZG_04_EDInt": "HZG_04_EDInt",
    "HZG_05_EDInt": "HZG_05_EDInt",
    "HZG_06_EDInt": "HZG_06_EDInt",
    "HZG_07_EDInt": "HZG_07_EDInt",
    "HZG_08_EDInt": "HZG_08_EDInt",
    "HZG_09_EDInt": "HZG_09_EDInt",
    "HZG_10_EDInt": "HZG_10_EDInt",
    "HZG_11_EDInt": "HZG_11_EDInt",
    "HZG_12_EDInt": "HZG_12_EDInt",
    "Energieeinleitung_Istwert": "SME (J/kg)",
}

# Older exports don't have these channels; they're written as blank columns instead of rejecting the file.
optional_columns = ["Vak_1_Anzeige", "Energieeinleitung_Istwert"] + [f"HZG_{i:02d}_EDInt" for i in range(13)]
required_columns = ["TIME"] + [c for c in column_names if c not in optional_columns]

data_frames_to_delete = []
for df_name, df in dfs.items():
  missing_required = [col for col in required_columns if col not in df.columns]
  if missing_required:
    print(f"Warning: {df_name} skipped, missing required column(s): {', '.join(missing_required)}")
    data_frames_to_delete.append(df_name)
    continue
  missing_optional = [col for col in optional_columns if col not in df.columns]
  if missing_optional:
    print(f"Note: {df_name} has no {', '.join(missing_optional)}; written as blank.")
  for col in missing_optional:
    df[col] = np.nan
  # One bad file is reported and skipped rather than stopping the whole run
  try:
    df = df.astype({col: float for col in column_names})
    df['TIME'] = pd.to_datetime(df['TIME'], format='%m/%d/%Y %I:%M:%S %p %z')
  except Exception as e:
    print(f"Warning: {df_name} skipped, could not convert values: {e}")
    data_frames_to_delete.append(df_name)
    continue
  df['Date'] = df['TIME'].dt.strftime('%Y-%m-%d')
  df['Time'] = df['TIME'].dt.strftime('%H:%M:%S')
  df = df[["Date", "Time"] + list(column_names)]
  df = df.rename(columns=column_names)
  df.insert(0, 'Pellet ID', df_name.split('.')[0])
  dfs[df_name] = df
  df.to_csv(os.path.join(output_folder, f'{df_name}.csv'), index=False)
for df_name in data_frames_to_delete:
  del dfs[df_name]
