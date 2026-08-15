use std::collections::HashMap;

pub struct Config {
    entries: HashMap<String, String>,
}

pub trait Parse {
    fn parse(&self, text: &str) -> usize;
}

impl Config {
    pub fn new() -> Self {
        Config {
            entries: HashMap::new(),
        }
    }

    pub fn set(&mut self, key: &str, value: &str) {
        self.entries.insert(key.to_string(), value.to_string());
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }
}

impl Parse for Config {
    fn parse(&self, text: &str) -> usize {
        text.lines().count() + self.len()
    }
}

pub fn load(text: &str) -> usize {
    let mut config = Config::new();
    config.set("source", text);
    config.parse(text)
}
