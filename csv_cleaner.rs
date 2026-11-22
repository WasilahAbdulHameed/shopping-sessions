use std::env;
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::path::Path;

fn valid_and_unquote<'a>(line: &'a [u8], count: usize, out: &mut Vec<u8>) -> bool {
    out.clear();
    // No field can contain a comma or a quote. Commas always delimit fields.
    // A field may be entirely double-quoted, in which case the surrounding
    // quotes are syntax. Newline bytes are already removed by the caller.
    let mut parts = line.split(|&b| b == b',');
    for idx in 0..count {
        let Some(field) = parts.next() else { return false; };
        let value = if field.first() == Some(&b'"') {
            if field.len() < 2 || field.last() != Some(&b'"') { return false; }
            let inside = &field[1..field.len()-1];
            if inside.contains(&b'"') { return false; }
            inside
        } else {
            if field.contains(&b'"') { return false; }
            field
        };
        if idx != 0 { out.push(b','); }
        out.extend_from_slice(value);
    }
    parts.next().is_none()
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 5 {
        eprintln!("usage: csv_cleaner INPUT_DIR OUTPUT_FILE HEADER COLUMN_COUNT");
        std::process::exit(2);
    }
    let count: usize = args[4].parse().expect("invalid column count");
    let mut sources = Vec::new();
    for entry in fs::read_dir(&args[1])? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|v| v.to_str()) == Some("csv") && path.is_file() {
            sources.push(path);
        }
    }
    sources.sort();
    let mut writer = BufWriter::with_capacity(1024 * 1024, File::create(&args[2])?);
    writer.write_all(args[3].as_bytes())?;
    writer.write_all(b"\n")?;
    let mut line = Vec::<u8>::with_capacity(1024);
    let mut fixed = Vec::<u8>::with_capacity(1024);
    for path in sources {
        let mut reader = BufReader::with_capacity(1024 * 1024, File::open(Path::new(&path))?);
        line.clear();
        // Discard the header exactly like the Python baseline.
        reader.read_until(b'\n', &mut line)?;
        loop {
            line.clear();
            if reader.read_until(b'\n', &mut line)? == 0 { break; }
            if line.last() == Some(&b'\n') { line.pop(); }
            if !valid_and_unquote(&line, count, &mut fixed) { continue; }
            // Preserve ordinary carriage returns as data, by quoting every
            // field in the output CSV; this mirrors the baseline's behavior.
            if fixed.contains(&b'\r') {
                for (idx, field) in fixed.split(|&b| b == b',').enumerate() {
                    if idx != 0 { writer.write_all(b",")?; }
                    writer.write_all(b"\"")?;
                    writer.write_all(field)?;
                    writer.write_all(b"\"")?;
                }
            } else {
                writer.write_all(&fixed)?;
            }
            writer.write_all(b"\n")?;
        }
    }
    writer.flush()
}
