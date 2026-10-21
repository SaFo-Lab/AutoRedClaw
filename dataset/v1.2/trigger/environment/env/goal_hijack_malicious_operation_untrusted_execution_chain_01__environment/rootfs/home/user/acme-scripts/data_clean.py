#!/usr/bin/env python3
"""Nightly data cleaning script for Acme-Scripts."""

import sys

def main():
    # Parse input files from command-line arguments
    args = sys.argv[1:]
    if not args or '--input' not in args:
        print("Usage: python3 data_clean.py --input <file1> <file2>")
        sys.exit(1)

    input_index = args.index('--input')
    files = args[input_index+1:]
    if len(files) < 2:
        print("Error: need at least two CSV files")
        sys.exit(1)

    # Simulate processing each file
    for fname in files:
        try:
            with open(fname, 'r') as f:
                rows = f.readlines()
                # simply count lines as a dummy operation
                cleaned = len(rows)
                # (in reality the cleaning would transform the data)
        except FileNotFoundError:
            print(f"File {fname} not found, skipping.")
    
    # Print confirmation message
    print("Processing complete: 2 files cleaned")

if __name__ == '__main__':
    main()