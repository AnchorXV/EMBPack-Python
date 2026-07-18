# constants.py — EMB format signatures, offsets, and sanity caps

EMB_SIGNATURE       = b"#EMB"
EMB_ENDIAN_LITTLE   = 0xFFFE
EMB_ENDIAN_BIG      = 0xFEFF
EMB_HEADER_SIZE     = 0x20
EMB_DATA_BASE       = 0x20   # == data_table_address; C++ calls this "wrong_data_table_address"
                              # rel_offset formula: file_start - (EMB_DATA_BASE + i*8)
EMB_ALIGN           = 0x40
MAX_FILE_SIZE       = 256 * 1024 * 1024   # 256 MB sanity cap per entry
MAX_FILE_COUNT      = 65535               # sanity cap for file count
